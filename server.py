"""
OffMsg Server v4 - исправлен push сообщений и статус онлайн
"""
from flask import Flask, request, jsonify, send_from_directory
from flask_sock import Sock
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import bcrypt, os, json, threading

app  = Flask(__name__)
sock = Sock(app)

app.config['SECRET_KEY']              = os.environ.get('SECRET_KEY', 'offmsg-2024')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///offmsg.db')
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = \
        app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

db = SQLAlchemy(app)

# ── Models ──────────────────────────────────────────
class User(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(32), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)

class Message(db.Model):
    id        = db.Column(db.Integer, primary_key=True)
    sender    = db.Column(db.String(32), nullable=False)
    recipient = db.Column(db.String(32), nullable=False)
    text      = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    read      = db.Column(db.Boolean, default=False)

class Contact(db.Model):
    id      = db.Column(db.Integer, primary_key=True)
    owner   = db.Column(db.String(32), nullable=False)
    contact = db.Column(db.String(32), nullable=False)

# ── Online users ────────────────────────────────────
_lock   = threading.Lock()
_online = {}  # username -> ws

def push(username, data):
    """Отправить данные пользователю если онлайн"""
    with _lock:
        ws = _online.get(username)
    if not ws:
        return False
    try:
        ws.send(json.dumps(data, ensure_ascii=False))
        return True
    except Exception:
        with _lock:
            _online.pop(username, None)
        return False

def notify_contacts_status(username, online):
    """Уведомить все контакты об изменении статуса"""
    try:
        contacts = Contact.query.filter_by(contact=username).all()
        ev = 'contact_online' if online else 'contact_offline'
        for c in contacts:
            push(c.owner, {'event': ev, 'data': {'username': username}})
    except Exception:
        pass

# ── CORS ────────────────────────────────────────────
@app.after_request
def cors(r):
    r.headers['Access-Control-Allow-Origin']  = '*'
    r.headers['Access-Control-Allow-Methods'] = 'GET,POST,DELETE,OPTIONS'
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return r

@app.route('/', defaults={'p': ''}, methods=['OPTIONS'])
@app.route('/<path:p>', methods=['OPTIONS'])
def options(p):
    from flask import Response
    return Response('', 200, {
        'Access-Control-Allow-Origin':  '*',
        'Access-Control-Allow-Methods': 'GET,POST,DELETE,OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type'
    })

# ── HTTP routes ─────────────────────────────────────
@app.route('/')
def index():
    with _lock:
        cnt = len(_online)
    return jsonify({'status': 'OffMsg v4', 'online': cnt})

@app.route('/register', methods=['POST'])
def register():
    d = request.json or {}
    username = d.get('username', '').strip()
    password = d.get('password', '')
    if not username or not password:
        return jsonify({'ok': False, 'error': 'Заполните все поля'})
    if len(username) < 3:
        return jsonify({'ok': False, 'error': 'Имя минимум 3 символа'})
    if len(password) < 4:
        return jsonify({'ok': False, 'error': 'Пароль минимум 4 символа'})
    if User.query.filter_by(username=username).first():
        return jsonify({'ok': False, 'error': 'Имя занято'})
    pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    db.session.add(User(username=username, password_hash=pw))
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/login', methods=['POST'])
def login():
    d = request.json or {}
    username = d.get('username', '').strip()
    password = d.get('password', '')
    user = User.query.filter_by(username=username).first()
    if not user:
        return jsonify({'ok': False, 'error': 'Пользователь не найден'})
    if not bcrypt.checkpw(password.encode(), user.password_hash.encode()):
        return jsonify({'ok': False, 'error': 'Неверный пароль'})
    return jsonify({'ok': True, 'username': username})

@app.route('/account', methods=['DELETE'])
def delete_account():
    username = request.args.get('username') or (request.json or {}).get('username')
    if not username:
        return jsonify({'ok': False})
    User.query.filter_by(username=username).delete()
    Message.query.filter((Message.sender == username) | (Message.recipient == username)).delete()
    Contact.query.filter((Contact.owner == username) | (Contact.contact == username)).delete()
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/history')
def history():
    me    = request.args.get('me')
    other = request.args.get('other')
    msgs  = Message.query.filter(
        ((Message.sender == me) & (Message.recipient == other)) |
        ((Message.sender == other) & (Message.recipient == me))
    ).order_by(Message.timestamp).all()
    for m in msgs:
        if m.recipient == me and not m.read:
            m.read = True
    db.session.commit()
    return jsonify([{
        'id': m.id, 'sender': m.sender, 'recipient': m.recipient,
        'text': m.text, 'timestamp': m.timestamp.isoformat(), 'read': m.read
    } for m in msgs])

@app.route('/contacts')
def get_contacts():
    owner = request.args.get('username')
    with _lock:
        online_now = set(_online.keys())
    return jsonify([{
        'username': c.contact,
        'online':   c.contact in online_now
    } for c in Contact.query.filter_by(owner=owner).all()])

@app.route('/contacts/add', methods=['POST'])
def add_contact():
    d       = request.json or {}
    owner   = d.get('owner')
    contact = d.get('contact', '').strip()
    if not User.query.filter_by(username=contact).first():
        return jsonify({'ok': False, 'error': 'Пользователь не найден'})
    if contact == owner:
        return jsonify({'ok': False, 'error': 'Нельзя добавить себя'})
    if Contact.query.filter_by(owner=owner, contact=contact).first():
        return jsonify({'ok': False, 'error': 'Уже в контактах'})
    db.session.add(Contact(owner=owner, contact=contact))
    db.session.commit()
    with _lock:
        is_online = contact in _online
    return jsonify({'ok': True, 'online': is_online})

@app.route('/unread')
def unread():
    username = request.args.get('username')
    msgs     = Message.query.filter_by(recipient=username, read=False).all()
    counts   = {}
    for m in msgs:
        counts[m.sender] = counts.get(m.sender, 0) + 1
    return jsonify(counts)

@app.route('/send', methods=['POST'])
def send_http():
    """HTTP fallback для отправки сообщений"""
    d         = request.json or {}
    sender    = d.get('sender')
    recipient = d.get('recipient')
    text      = (d.get('text') or '').strip()
    if not text or not sender or not recipient:
        return jsonify({'ok': False})
    m = Message(sender=sender, recipient=recipient, text=text)
    db.session.add(m)
    db.session.commit()
    payload = {
        'id': m.id, 'sender': sender, 'recipient': recipient,
        'text': text, 'timestamp': m.timestamp.isoformat()
    }
    # Push через WebSocket немедленно
    push(recipient, {'event': 'new_message',  'data': payload})
    push(sender,    {'event': 'message_sent', 'data': payload})
    return jsonify({'ok': True, 'id': m.id})

@app.route('/upload', methods=['POST'])
def upload():
    sender    = request.form.get('sender')
    recipient = request.form.get('recipient')
    file      = request.files.get('file')
    if not file or not sender or not recipient:
        return jsonify({'ok': False}), 400
    safe = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{file.filename}"
    file.save(os.path.join(UPLOAD_FOLDER, safe))
    m = Message(sender=sender, recipient=recipient, text=f'[FILE]{safe}')
    db.session.add(m)
    db.session.commit()
    payload = {
        'id': m.id, 'sender': sender, 'recipient': recipient,
        'text': m.text, 'timestamp': m.timestamp.isoformat()
    }
    push(recipient, {'event': 'new_message',  'data': payload})
    push(sender,    {'event': 'message_sent', 'data': payload})
    return jsonify({'ok': True, 'filename': safe})

@app.route('/files/<filename>')
def get_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/app')
def web_app():
    return send_from_directory('static', 'index.html')

# ── WebSocket ────────────────────────────────────────
@sock.route('/ws')
def websocket(ws):
    username = None
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            ev = msg.get('event')

            if ev == 'auth':
                username = msg.get('username', '').strip()
                if not username:
                    continue
                with _lock:
                    _online[username] = ws
                # Уведомить контакты что онлайн
                notify_contacts_status(username, True)
                # Подтвердить подключение
                try:
                    ws.send(json.dumps({'event': 'auth_ok', 'data': {'username': username}}))
                except Exception:
                    pass

            elif ev == 'send_message':
                sender    = msg.get('sender', '').strip()
                recipient = msg.get('recipient', '').strip()
                text      = (msg.get('text') or '').strip()
                if not text or not sender or not recipient:
                    continue
                m = Message(sender=sender, recipient=recipient, text=text)
                db.session.add(m)
                db.session.commit()
                payload = {
                    'id': m.id, 'sender': sender, 'recipient': recipient,
                    'text': text, 'timestamp': m.timestamp.isoformat()
                }
                # Немедленно пушим обоим
                push(recipient, {'event': 'new_message',  'data': payload})
                push(sender,    {'event': 'message_sent', 'data': payload})

            elif ev == 'ping':
                try:
                    ws.send(json.dumps({'event': 'pong'}))
                except Exception:
                    break

    except Exception:
        pass
    finally:
        if username:
            with _lock:
                _online.pop(username, None)
            notify_contacts_status(username, False)

# ── Init ─────────────────────────────────────────────
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
