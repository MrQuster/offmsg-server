"""
OffMsg Server v3 - Flask + flask-sock (чистый WebSocket)
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

_lock   = threading.Lock()
_online = {}

def send_to(username, data):
    with _lock:
        ws = _online.get(username)
    if ws:
        try:
            ws.send(json.dumps(data))
            return True
        except Exception:
            with _lock:
                _online.pop(username, None)
    return False

@app.route('/')
def index():
    return jsonify({'status': 'OffMsg v3', 'online': len(_online)})

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
    return jsonify([{
        'username': c.contact,
        'online':   c.contact in _online
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
    return jsonify({'ok': True, 'online': contact in _online})

@app.route('/unread')
def unread():
    username = request.args.get('username')
    msgs     = Message.query.filter_by(recipient=username, read=False).all()
    counts   = {}
    for m in msgs:
        counts[m.sender] = counts.get(m.sender, 0) + 1
    return jsonify(counts)

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
    payload = {'id': m.id, 'sender': sender, 'recipient': recipient,
               'text': m.text, 'timestamp': m.timestamp.isoformat()}
    send_to(recipient, {'event': 'new_message',  'data': payload})
    send_to(sender,    {'event': 'message_sent', 'data': payload})
    return jsonify({'ok': True, 'filename': safe})

@app.route('/files/<filename>')
def get_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

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

            event = msg.get('event')

            if event == 'auth':
                username = msg.get('username')
                if not username:
                    continue
                with _lock:
                    _online[username] = ws
                for c in Contact.query.filter_by(contact=username).all():
                    send_to(c.owner, {'event': 'contact_online', 'data': {'username': username}})

            elif event == 'send_message':
                sender    = msg.get('sender')
                recipient = msg.get('recipient')
                text      = (msg.get('text') or '').strip()
                if not text or not sender or not recipient:
                    continue
                m = Message(sender=sender, recipient=recipient, text=text)
                db.session.add(m)
                db.session.commit()
                payload = {'id': m.id, 'sender': sender, 'recipient': recipient,
                           'text': text, 'timestamp': m.timestamp.isoformat()}
                send_to(recipient, {'event': 'new_message',  'data': payload})
                send_to(sender,    {'event': 'message_sent', 'data': payload})

    except Exception:
        pass
    finally:
        if username:
            with _lock:
                _online.pop(username, None)
            try:
                for c in Contact.query.filter_by(contact=username).all():
                    send_to(c.owner, {'event': 'contact_offline', 'data': {'username': username}})
            except Exception:
                pass


@app.route('/send', methods=['POST'])
def send_msg():
    d         = request.json or {}
    sender    = d.get('sender')
    recipient = d.get('recipient')
    text      = (d.get('text') or '').strip()
    if not text or not sender or not recipient:
        return jsonify({'ok': False})
    m = Message(sender=sender, recipient=recipient, text=text)
    db.session.add(m)
    db.session.commit()
    payload = {'id': m.id, 'sender': sender, 'recipient': recipient,
               'text': text, 'timestamp': m.timestamp.isoformat()}
    send_to(recipient, {'event': 'new_message',  'data': payload})
    send_to(sender,    {'event': 'message_sent', 'data': payload})
    return jsonify({'ok': True, 'id': m.id})


@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin']  = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,DELETE,OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options(path):
    from flask import Response
    return Response('', status=200, headers={
        'Access-Control-Allow-Origin':  '*',
        'Access-Control-Allow-Methods': 'GET,POST,DELETE,OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type'
    })


@app.route('/app')
def web_app():
    return send_from_directory('static', 'index.html')

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
