"""Evidence-based receipt checks. No invented oracle fields or fault-index mapping."""
import base64
import binascii

from fastapi import HTTPException

from . import storage as store

MAX_BATCHES = 1_000_000


def _integer(value, name, limit=2**63 - 1):
    if type(value) is not int or not 0 <= value <= limit:
        raise HTTPException(422, 'Invalid receipt ' + name)
    return value


def bitmap(value, name, capacity):
    if not isinstance(value, str) or len(value) > ((MAX_BATCHES + 7) // 8 + 2) // 3 * 4:
        raise HTTPException(422, 'Invalid or oversized ' + name)
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, 'Invalid base64 ' + name)
    if len(raw) > (capacity + 7) // 8:
        raise HTTPException(422, 'Bitmap exceeds configured batch capacity')
    values = set()
    for byte_index, byte in enumerate(raw):
        while byte:
            low = byte & -byte
            bit = low.bit_length() - 1
            index = byte_index * 8 + bit
            if index >= capacity:
                raise HTTPException(422, 'Bitmap sets a bit beyond batch capacity')
            values.add(index)
            byte ^= low
    return values


def reconcile_receipt(receipt):
    if not isinstance(receipt, dict):
        raise HTTPException(422, 'Receipt must be an object')
    config, stats = receipt.get('config'), receipt.get('stats')
    if not isinstance(config, dict) or not isinstance(stats, dict):
        raise HTTPException(422, 'Receipt requires config and stats')
    run = config.get('test_id')
    if not isinstance(run, str) or not 1 <= len(run) <= 128:
        raise HTTPException(422, 'Invalid receipt test_id')
    if config.get('workload') != 'objects' or receipt.get('bitmap_encoding') != 'lsb0':
        raise HTTPException(422, 'Only objects workload with lsb0 bitmaps is supported')
    capacity = _integer(config.get('max_batches'), 'max_batches', MAX_BATCHES)
    if not capacity:
        raise HTTPException(422, 'max_batches must be positive')
    ack = bitmap(receipt.get('ack_bitmap'), 'ack_bitmap', capacity)
    sent = bitmap(receipt.get('sent_bitmap'), 'sent_bitmap', capacity)
    if not ack.issubset(sent):
        raise HTTPException(422, 'Acknowledged bitmap is not a subset of sent bitmap')
    ack_count = _integer(stats.get('acknowledged_batches'), 'acknowledged_batches')
    ack_objects = _integer(stats.get('acknowledged_objects'), 'acknowledged_objects')
    tree = receipt.get('tree', {})
    if not isinstance(tree, dict):
        raise HTTPException(422, 'Invalid tree receipt')
    ack_revision = tree.get('acknowledged_revision', stats.get('tree_acknowledged_revision'))
    if ack_revision is not None:
        ack_revision = _integer(ack_revision, 'tree acknowledgement')

    with store.connect() as db:
        db.execute('BEGIN')
        selected = db.execute('SELECT * FROM runs WHERE id=?', (run,)).fetchone()
        if not selected:
            raise HTTPException(404, 'Receipt run does not exist locally')
        stored = set()
        stored_ack_objects = 0
        for row in db.execute("SELECT key,objects FROM messages WHERE run=? AND kind='objects'", (run,)):
            # Valid main batches are within configured capacity. Probe namespaces are not guessed.
            if 0 <= row['key'] < capacity:
                stored.add(row['key'])
            if row['key'] in ack:
                stored_ack_objects += row['objects']
        object_rows = db.execute('SELECT COUNT(*) FROM objects WHERE run=?', (run,)).fetchone()[0]
        type_total = db.execute('SELECT COALESCE(SUM(count),0) FROM types WHERE run=?', (run,)).fetchone()[0]
        tree_rows = db.execute("SELECT COUNT(*),COALESCE(MIN(key),0),COALESCE(MAX(key),0) FROM messages WHERE run=? AND kind='types'", (run,)).fetchone()
        rejected_distinct = db.execute('SELECT COUNT(*) FROM rejected WHERE run=?', (run,)).fetchone()[0]

    checks = []
    def check(name, expected, actual, condition=None, note=None):
        status = 'unknown' if condition is None else ('pass' if condition else 'fail')
        item = {'name': name, 'expected': expected, 'actual': actual, 'status': status}
        if note:
            item['note'] = note
        checks.append(item)

    missing = ack - stored
    unconfirmed = (stored & sent) - ack
    check('回执确认批次数与 bitmap 一致', ack_count, len(ack), ack_count == len(ack))
    check('已确认批次缺失数', 0, len(missing), not missing)
    check('已确认主批次对象记录数', ack_objects, stored_ack_objects, ack_objects == stored_ack_objects,
          '按 ack_bitmap 指定批次求和；不把额外探针计入主流对象数。')
    check('已入库但回执未确认的主批次', 0, len(unconfirmed), True if not unconfirmed else None,
          '可能发生在提交成功但响应丢失时；这不等于数据丢失。')
    check('对象计数与持久化行数一致', selected['objects'], object_rows, selected['objects'] == object_rows)
    check('直属分类计数与对象行数一致', object_rows, type_total, type_total == object_rows)
    current_revision = selected['revision']
    tree_contiguous = (tree_rows[0] == current_revision and tree_rows[2] == current_revision
                       and (current_revision == 0 or tree_rows[1] == 1))
    check('本地分类版本连续保存', current_revision, tree_rows[0], tree_contiguous)
    check('已确认分类版本已保存', ack_revision, current_revision,
          current_revision >= ack_revision if ack_revision is not None else None,
          '允许本地版本领先回执：提交后确认响应可能丢失。')

    injections = receipt.get('injections')
    if injections is not None and not isinstance(injections, dict):
        raise HTTPException(422, 'Invalid injections receipt')
    if injections and 'fault_ack_bitmap' in injections:
        fault_ack = bitmap(injections['fault_ack_bitmap'], 'fault_ack_bitmap', capacity)
        fault_sent = bitmap(injections.get('fault_sent_bitmap', ''), 'fault_sent_bitmap', capacity)
        fault_rejected = bitmap(injections.get('fault_rejected_bitmap', ''), 'fault_rejected_bitmap', capacity)
        if not fault_ack.issubset(fault_sent) or not fault_rejected.issubset(fault_sent):
            raise HTTPException(422, 'Invalid fault bitmap relationship')
        check('异常探针未获成功确认（本系统策略）', 0, len(fault_ack), not fault_ack,
              '这是结构校验政策，并非声称掌握官方隐藏评分规则。')
        check('发生器记录的异常探针拒收', len(fault_sent), len(fault_rejected),
              fault_rejected == fault_sent,
              '仅核对发生器回执；网络失败可能使探针既未确认也未明确拒收。')
        check('隔离记录与探针逐条对应', '逐条身份对应', rejected_distinct, None,
              '尚未建立 source-batch bitmap 与 probe batch_id 的经过验证的映射，不能宣称逐条对账通过。')
    else:
        check('异常探针拒收', '回执包含探针记录', None, None)
    if injections and 'duplicate_ack_bitmap' in injections:
        duplicate_ack = bitmap(injections['duplicate_ack_bitmap'], 'duplicate_ack_bitmap', capacity)
        check('重复确认与本地吸收计数', len(duplicate_ack), selected['duplicates'],
              selected['duplicates'] >= len(duplicate_ack),
              '本地计数可能包含网络重试；SQL 唯一约束保证相同批次不会重复应用。')
    check('对象内容及各类型分布的独立校验', '离线重放参考', None, None,
          '运行 receipt 不含逐对象内容或类型频率；计数对账不能替代内容完整性验证。')
    return {'run': run, 'ok': all(c['status'] != 'fail' for c in checks),
            'complete': all(c['status'] == 'pass' for c in checks),
            'checks': checks, 'missing_batch_ids': sorted(missing)[:100],
            'unconfirmed_batch_ids': sorted(unconfirmed)[:100],
            'limits': ['Only the first 100 differing IDs are returned.',
                       'ok means no implemented check failed; complete is false when any check is unknown.',
                       'Receipt counters do not prove object-content equality.'],
            'confirmed_batches': len(ack), 'stored_main_batches': len(stored)}
