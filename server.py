"""
OffMsg Server v2 — с поддержкой файлов и удаления аккаунта
"""
from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, join_room
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import bcrypt, os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'offmsg-secret-2024')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///messenger.db')
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender = db.Column(db.String(32), nullable=False)
    recipient = db.Column(db.String(32), nullable=False)
    text = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    read = db.Column(db.Boolean, default=False)

class Contact(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner = db.Column(db.String(32), nullable=False)
    contact = db.Column(db.String(32), nullable=False)

online_users = {}

@app.route('/')
def index():
    return jsonify({'status': 'OffMsg server v2 ✓'})

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username or not password:
        return jsonify({'ok': False, 'error': 'Заполните все поля'})
    if len(username) < 3:
        return jsonify({'ok': False, 'error': 'Имя минимум 3 символа'})
    if len(password) < 4:
        return jsonify({'ok': False, 'error': 'Пароль минимум 4 символа'})
    if User.query.filter_by(username=username).first():
        return jsonify({'ok': False, 'error': 'Имя занято'})
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    db.session.add(User(username=username, password_hash=pw_hash))
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
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
        return jsonify({'ok': False, 'error': 'Не указан пользователь'})
    User.query.filter_by(username=username).delete()
    Message.query.filter((Message.sender == username) | (Message.recipient == username)).delete()
    Contact.query.filter((Contact.owner == username) | (Contact.contact == username)).delete()
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/history')
def history():
    me = request.args.get('me')
    other = request.args.get('other')
    msgs = Message.query.filter(
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
    contacts = Contact.query.filter_by(owner=owner).all()
    return jsonify([{'username': c.contact, 'online': c.contact in online_users} for c in contacts])

@app.route('/contacts/add', methods=['POST'])
def add_contact():
    data = request.json
    owner = data.get('owner')
    contact = data.get('contact', '').strip()
    if not User.query.filter_by(username=contact).first():
        return jsonify({'ok': False, 'error': 'Пользователь не найден'})
    if contact == owner:
        return jsonify({'ok': False, 'error': 'Нельзя добавить себя'})
    if Contact.query.filter_by(owner=owner, contact=contact).first():
        return jsonify({'ok': False, 'error': 'Уже в контактах'})
    db.session.add(Contact(owner=owner, contact=contact))
    db.session.commit()
    return jsonify({'ok': True, 'online': contact in online_users})

@app.route('/unread')
def unread():
    username = request.args.get('username')
    msgs = Message.query.filter_by(recipient=username, read=False).all()
    counts = {}
    for m in msgs:
        counts[m.sender] = counts.get(m.sender, 0) + 1
    return jsonify(counts)

@app.route('/upload', methods=['POST'])
def upload():
    sender = request.form.get('sender')
    recipient = request.form.get('recipient')
    file = request.files.get('file')
    if not file or not sender or not recipient:
        return jsonify({'ok': False, 'error': 'Неверные данные'}), 400

    safe_name = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{file.filename}"
    file.save(os.path.join(UPLOAD_FOLDER, safe_name))

    msg = Message(sender=sender, recipient=recipient, text=f"[FILE]{safe_name}")
    db.session.add(msg)
    db.session.commit()

    payload = {
        'id': msg.id, 'sender': sender, 'recipient': recipient,
        'text': msg.text, 'timestamp': msg.timestamp.isoformat()
    }
    if recipient in online_users:
        socketio.emit('new_message', payload, room=recipient)
    if sender in online_users:
        socketio.emit('message_sent', payload, room=sender)

    return jsonify({'ok': True, 'filename': safe_name})

@app.route('/files/<filename>')
def get_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@socketio.on('auth')
def on_auth(data):
    username = data.get('username')
    if username:
        online_users[username] = request.sid
        join_room(username)
        contacts = Contact.query.filter_by(contact=username).all()
        for c in contacts:
            if c.owner in online_users:
                emit('contact_online', {'username': username}, room=c.owner)

@socketio.on('disconnect')
def on_disconnect():
    username = None
    for u, sid in list(online_users.items()):
        if sid == request.sid:
            username = u
            del online_users[u]
            break
    if username:
        contacts = Contact.query.filter_by(contact=username).all()
        for c in contacts:
            if c.owner in online_users:
                emit('contact_offline', {'username': username}, room=c.owner)

@socketio.on('send_message')
def on_message(data):
    sender = data.get('sender')
    recipient = data.get('recipient')
    text = data.get('text', '').strip()
    if not text:
        return
    msg = Message(sender=sender, recipient=recipient, text=text)
    db.session.add(msg)
    db.session.commit()
    payload = {'id': msg.id, 'sender': sender, 'recipient': recipient,
               'text': text, 'timestamp': msg.timestamp.isoformat()}
    if recipient in online_users:
        emit('new_message', payload, room=recipient)
    emit('message_sent', payload, room=sender)

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
