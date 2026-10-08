"""Gate10 local demonstration. Python 3.10+, standard library only."""
import argparse
import datetime as dt
import hashlib
import hmac
import json
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.cookies import SimpleCookie
from urllib.parse import urlparse

ROOT = Path(__file__).parent
JST = dt.timezone(dt.timedelta(hours=9))
STAGES = ['初回訪問', '提案中', '見積提出済み', '内諾', '受注', '失注']
ACTIONS = ['訪問', '見積送付', '電話']
REACTIONS = ['👍', '詳しく聞かせて', '同行しようか']
FIELDS = ['customer', 'title', 'stage', 'action', 'due', 'amount', 'expected', 'memo']
SESSIONS = {}
FAILURES = {}
DB = ROOT / 'data/gate10.sqlite3'

def now():
    return dt.datetime.now(JST).isoformat(timespec='seconds')

def today():
    return dt.datetime.now(JST).date()

class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()

def connect():
    c = sqlite3.connect(DB, timeout=10, factory=Connection)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c

def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 120000).hex()

def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, role TEXT, login TEXT UNIQUE, salt TEXT, hash TEXT);
        CREATE TABLE IF NOT EXISTS deals(id INTEGER PRIMARY KEY, owner INTEGER REFERENCES users(id), customer TEXT, title TEXT, stage TEXT, action TEXT, due TEXT, amount INTEGER, expected TEXT, memo TEXT, updated TEXT, version INTEGER DEFAULT 1, won_at TEXT);
        CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, deal INTEGER REFERENCES deals(id), actor INTEGER REFERENCES users(id), at TEXT, version INTEGER, before_json TEXT, after_json TEXT);
        CREATE TABLE IF NOT EXISTS feedback(deal INTEGER REFERENCES deals(id), version INTEGER, reader INTEGER REFERENCES users(id), read_at TEXT, reaction TEXT, reacted_at TEXT, PRIMARY KEY(deal,version));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, at TEXT, login TEXT, success INTEGER);
        ''')
        if c.execute('SELECT count(*) FROM users').fetchone()[0]:
            return
        for i in range(1, 14):
            salt = secrets.token_hex(16)
            c.execute('INSERT INTO users VALUES(?,?,?,?,?,?)', (i, '営業部長' if i == 13 else f'営業 {i:02}', 'manager' if i == 13 else 'sales', 'manager' if i == 13 else f'sales{i:02}', salt, password_hash('Gate10Demo1234', salt)))
        for i in range(1, 25):
            stage = STAGES[(i-1) % 6]
            stamp = (dt.datetime.now(JST)-dt.timedelta(days=i % 11)).isoformat(timespec='seconds')
            vals = ((i-1)%12+1, f'架空商事 {i:02}', ['業務改善支援','営業管理導入','保守更新'][i%3], stage, ACTIONS[i%3], (today()+dt.timedelta(days=i%9-4)).isoformat(), None if i%7 == 0 else i*150000, None if i%8 == 0 else (today()+dt.timedelta(days=i%35)).isoformat(), 'デモ用の架空案件です', stamp, 1, stamp if stage == '受注' else None)
            c.execute('INSERT INTO deals(owner,customer,title,stage,action,due,amount,expected,memo,updated,version,won_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', vals)
            row = dict(c.execute('SELECT * FROM deals WHERE id=?',(i,)).fetchone())
            c.execute('INSERT INTO history(deal,actor,at,version,before_json,after_json) VALUES(?,?,?,?,?,?)',(i,row['owner'],stamp,1,'{}',json.dumps(row,ensure_ascii=False)))

class Problem(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message

def validate(data):
    clean = {}
    for key in ['customer','title','stage','action','due','expected','memo']:
        value = data.get(key, '')
        if not isinstance(value,str):
            raise Problem(422,'入力形式が正しくありません。')
        clean[key] = value.strip()
    if not clean['customer'] or not clean['title']:
        raise Problem(422,'顧客名と案件名を入力してください。')
    if any(len(clean[k]) > 100 for k in ['customer','title']) or len(clean['memo']) > 140:
        raise Problem(422,'顧客名・案件名は100文字、一言メモは140文字以内です。')
    if clean['stage'] not in STAGES or clean['action'] not in ACTIONS:
        raise Problem(422,'ステージと次のアクションを選択してください。')
    for k in ['due','expected']:
        try:
            if clean[k] or k == 'due':
                dt.date.fromisoformat(clean[k])
        except ValueError:
            raise Problem(422,'有効な日付を入力してください。')
    amount = data.get('amount')
    if amount is None or amount == '':
        clean['amount'] = None
    elif isinstance(amount, bool) or not str(amount).isascii() or not str(amount).isdigit() or int(amount) > 999999999999:
        raise Problem(422,'金額は0以上999,999,999,999以下の整数です。未定なら空欄にしてください。')
    else:
        clean['amount'] = int(amount)
    clean['expected'] = clean['expected'] or None
    return clean

def visible(c, user, deal_id):
    row = c.execute('SELECT * FROM deals WHERE id=?',(deal_id,)).fetchone()
    if not row:
        raise Problem(404,'案件が見つかりません。')
    if user['role'] != 'manager' and row['owner'] != user['id']:
        raise Problem(403,'この案件へのアクセス権限がありません。')
    return dict(row)

def decorate(row):
    row = dict(row)
    active = row['stage'] not in ['受注','失注']
    row['stale'] = active and (dt.datetime.now(JST)-dt.datetime.fromisoformat(row['updated'])).total_seconds() >= 7*86400
    row['overdue'] = active and row['due'] < today().isoformat()
    row['reminder'] = active and row['due'] <= (today()+dt.timedelta(days=1)).isoformat()
    return row

class Handler(BaseHTTPRequestHandler):
    def reply(self,status,data):
        if status < 400 and getattr(self,'connection_db',None):
            self.connection_db.commit()
        raw = json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        if getattr(self,'cookie',None):
            self.send_header('Set-Cookie',self.cookie)
        self.end_headers()
        self.wfile.write(raw)

    def session(self):
        cookies = SimpleCookie(self.headers.get('Cookie',''))
        sid = cookies.get('gate10')
        sess = SESSIONS.get(sid.value if sid else '')
        if not sess or sess['expires'] < dt.datetime.now().timestamp():
            raise Problem(401,'ログインしてください。')
        return sess

    def do_GET(self):
        self.handle_request('GET')

    def do_POST(self):
        self.handle_request('POST')

    def handle_request(self,method):
        try:
            path = urlparse(self.path).path
            if not path.startswith('/api/'):
                files = {'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css')}
                if method != 'GET' or path not in files:
                    raise Problem(404,'ページがありません。')
                name,mime = files[path]
                raw = (ROOT/'static'/name).read_bytes()
                self.send_response(200)
                self.send_header('Content-Type',mime+'; charset=utf-8')
                self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(raw)
                return
            data = {}
            if method == 'POST':
                origin = self.headers.get('Origin')
                if origin and origin != 'http://'+self.headers.get('Host',''):
                    raise Problem(403,'送信元を確認できません。')
                length = int(self.headers.get('Content-Length','0'))
                if length > 16384:
                    raise Problem(413,'入力が長すぎます。')
                data = json.loads(self.rfile.read(length) or '{}')
                if not isinstance(data,dict):
                    raise Problem(400,'入力形式が正しくありません。')
            with connect() as c:
                self.connection_db = c
                if path == '/api/login' and method == 'POST':
                    login = str(data.get('login',''))[:100]
                    failed, until = FAILURES.get(login,(0,0))
                    if until > dt.datetime.now().timestamp():
                        raise Problem(429,'5回失敗しました。5分後に再試行してください。')
                    u = c.execute('SELECT * FROM users WHERE login=?',(login,)).fetchone()
                    success = bool(u and hmac.compare_digest(u['hash'],password_hash(str(data.get('password','')),u['salt'])))
                    c.execute('INSERT INTO audit(at,login,success) VALUES(?,?,?)',(now(),login,int(success)))
                    c.commit()
                    if not success:
                        failed += 1
                        FAILURES[login] = (failed,dt.datetime.now().timestamp()+300 if failed>=5 else 0)
                        raise Problem(401,'IDまたはパスワードが違います。デモの問い合わせ先は提出者です。')
                    FAILURES.pop(login,None)
                    user = {k:u[k] for k in ['id','name','role','login']}
                    sid,csrf = secrets.token_urlsafe(32),secrets.token_urlsafe(32)
                    SESSIONS[sid] = {'user':user,'csrf':csrf,'expires':dt.datetime.now().timestamp()+8*3600}
                    self.cookie = f'gate10={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'
                    return self.reply(200,{'user':user,'csrf':csrf})
                sess = self.session()
                user = sess['user']
                if method == 'POST' and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),sess['csrf']):
                    raise Problem(403,'ページを再読み込みしてください。')
                if path == '/api/me' and method == 'GET':
                    return self.reply(200,{'user':user,'csrf':sess['csrf']})
                if path == '/api/logout' and method == 'POST':
                    SESSIONS.pop(SimpleCookie(self.headers['Cookie'])['gate10'].value,None)
                    self.cookie = 'gate10=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'
                    return self.reply(200,{'ok':True})
                if path == '/api/deals' and method == 'GET':
                    rows = c.execute('''SELECT d.*,u.name owner_name,f.read_at,f.reaction FROM deals d JOIN users u ON u.id=d.owner LEFT JOIN feedback f ON f.deal=d.id AND f.version=d.version WHERE (?='manager' OR owner=?) ORDER BY due,id''',(user['role'],user['id'])).fetchall()
                    return self.reply(200,{'deals':[decorate(r) for r in rows],'today':today().isoformat()})
                if path == '/api/deals' and method == 'POST':
                    if user['role'] != 'sales':
                        raise Problem(403,'案件登録は営業が行います。')
                    clean = validate(data)
                    c.execute('BEGIN IMMEDIATE')
                    if c.execute('SELECT 1 FROM deals WHERE customer=? AND title=?',(clean['customer'],clean['title'])).fetchone():
                        raise Problem(409,'同じ顧客名・案件名の案件があります。重複を確認してください。')
                    stamp = now()
                    cur = c.execute('INSERT INTO deals(owner,customer,title,stage,action,due,amount,expected,memo,updated,won_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(user['id'],*[clean[k] for k in FIELDS],stamp,stamp if clean['stage']=='受注' else None))
                    deal_id = cur.lastrowid
                    after = visible(c,user,deal_id)
                    c.execute('INSERT INTO history(deal,actor,at,version,before_json,after_json) VALUES(?,?,?,?,?,?)',(deal_id,user['id'],stamp,1,'{}',json.dumps(after,ensure_ascii=False)))
                    return self.reply(201,{'id':deal_id})
                parts = path.split('/')
                if len(parts) in [4,5] and parts[2]=='deals' and parts[3].isdigit():
                    deal_id = int(parts[3])
                    if method == 'POST':
                        c.execute('BEGIN IMMEDIATE')
                    row = visible(c,user,deal_id)
                    if method == 'GET' and len(parts)==4:
                        history = [dict(r) for r in c.execute('SELECT h.*,u.name actor_name FROM history h JOIN users u ON u.id=h.actor WHERE deal=? ORDER BY h.id DESC',(deal_id,))]
                        f = c.execute('SELECT * FROM feedback WHERE deal=? AND version=?',(deal_id,row['version'])).fetchone()
                        return self.reply(200,{'deal':decorate(row),'history':history,'feedback':dict(f) if f else None})
                    if method == 'POST' and len(parts)==5 and parts[4] in ['read','react']:
                        if user['role'] != 'manager':
                            raise Problem(403,'部長のみ操作できます。')
                        if data.get('version') != row['version']:
                            raise Problem(409,'案件が更新されました。最新の内容を開き直してください。')
                        if parts[4]=='react' and data.get('reaction') not in REACTIONS:
                            raise Problem(422,'反応を選択してください。')
                        c.execute('INSERT INTO feedback(deal,version,reader,read_at) VALUES(?,?,?,?) ON CONFLICT(deal,version) DO NOTHING',(deal_id,row['version'],user['id'],now()))
                        if parts[4]=='react':
                            c.execute('UPDATE feedback SET reaction=?,reacted_at=? WHERE deal=? AND version=?',(data['reaction'],now(),deal_id,row['version']))
                        return self.reply(200,{'ok':True})
                    if method == 'POST' and len(parts)==4:
                        if user['role'] != 'sales':
                            raise Problem(403,'案件更新は担当営業が行います。')
                        if data.get('version') != row['version']:
                            raise Problem(409,'他の更新がありました。入力内容を控え、最新の案件を開き直してください。')
                        clean = validate(data)
                        if c.execute('SELECT 1 FROM deals WHERE customer=? AND title=? AND id<>?',(clean['customer'],clean['title'],deal_id)).fetchone():
                            raise Problem(409,'同じ顧客名・案件名の案件があります。')
                        stamp = now()
                        won = (row['won_at'] if row['stage']=='受注' else stamp) if clean['stage']=='受注' else None
                        c.execute('UPDATE deals SET customer=?,title=?,stage=?,action=?,due=?,amount=?,expected=?,memo=?,updated=?,version=version+1,won_at=? WHERE id=?',(*[clean[k] for k in FIELDS],stamp,won,deal_id))
                        after = visible(c,user,deal_id)
                        c.execute('INSERT INTO history(deal,actor,at,version,before_json,after_json) VALUES(?,?,?,?,?,?)',(deal_id,user['id'],stamp,after['version'],json.dumps(row,ensure_ascii=False),json.dumps(after,ensure_ascii=False)))
                        return self.reply(200,{'id':deal_id})
                raise Problem(404,'操作が見つかりません。')
        except Problem as e:
            self.reply(e.status,{'error':e.message})
        except (ValueError,TypeError):
            self.reply(400,{'error':'入力形式が正しくありません。'})
        except sqlite3.Error:
            self.reply(503,{'error':'保存できませんでした。しばらく待って再試行してください。'})

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8810)
    parser.add_argument('--db',type=Path,default=DB)
    args = parser.parse_args()
    DB = args.db
    initialize()
    print(f'Gate10: http://127.0.0.1:{args.port}',flush=True)
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
