import math
import re
from .storage import IngestError

def require(condition,reason):
    if not condition: raise IngestError(422,reason)

def integer(n,minimum=0):
    return type(n) is int and minimum<=n<2**63

def finite(n):
    try: return type(n) in (int,float) and math.isfinite(n)
    except OverflowError: return False

def validate(msg):
    require(isinstance(msg,dict),'Message must be an object')
    require(isinstance(msg.get('test_id'),str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',msg['test_id']),'Invalid test_id')
    require(msg.get('kind') in ('types','objects'),'Unsupported message kind')
    if msg['kind']=='types':
        require(integer(msg.get('revision'),1),'Invalid taxonomy revision')
        require(isinstance(msg.get('changes'),list) and len(msg['changes'])<=10000,'Invalid changes array')
        for c in msg['changes']:
            require(isinstance(c,dict) and c.get('op') in ('create','rename','move'),'Unsupported taxonomy operation')
            n=c.get('node');require(isinstance(n,dict),'Missing taxonomy node')
            require(integer(n.get('type_id'),1) and integer(n.get('parent_id')),'Invalid taxonomy node identifiers')
            require(isinstance(n.get('name'),str) and 0<len(n['name'])<=4096,'Invalid taxonomy name')
    else:
        require(integer(msg.get('batch_id')),'Invalid batch_id')
        require(integer(msg.get('tree_revision'),1),'Invalid tree_revision')
        require(isinstance(msg.get('objects'),list) and 0<len(msg['objects'])<=10000,'Invalid objects array')
        for o in msg['objects']:
            require(isinstance(o,dict),'Object record must be an object')
            require(isinstance(o.get('id'),str) and 0<len(o['id'])<=512,'Missing or invalid object id')
            require(integer(o.get('type_id'),1),'Missing or invalid type_id')
            require(integer(o.get('sequence')) and integer(o.get('timestamp_ms')),'Invalid sequence or timestamp_ms')
            require(isinstance(o.get('name'),str) and len(o['name'])<=4096,'Missing or invalid name')
            require(isinstance(o.get('attributes'),dict),'attributes must be an object')
            require('position' in o,'Missing position (null is allowed)')
            p=o['position']
            if p is not None:
                require(isinstance(p,dict),'position must be an object or null')
                require(all(isinstance(p.get(k),str) and 0<len(p[k])<=512 for k in ['frame_id','frame_kind','unit']),'Invalid position reference frame')
                require(all(finite(p.get(k)) for k in ['x','y','z']),'Invalid position coordinates')
