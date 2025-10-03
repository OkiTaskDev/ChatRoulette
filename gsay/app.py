from flask import Flask, render_template_string, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
import secrets
import time
from datetime import datetime, timedelta
import logging
import threading
import sqlite3
import os
import html
from typing import Dict, List, Optional, Set

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = secrets.token_hex(16)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', logger=False, engineio_logger=False)

DB_PATH = 'chatroulette.db'
LOG_DIR = 'logs_report'

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_reports (
            ip TEXT,
            report_count INTEGER DEFAULT 0,
            last_reported TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (ip)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_bans (
            ip TEXT PRIMARY KEY,
            ban_end TIMESTAMP,
            reason TEXT,
            ban_count INTEGER DEFAULT 0
        )
    ''')
    try:
        cursor.execute('ALTER TABLE user_bans ADD COLUMN ban_count INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS report_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reported_ip TEXT,
            reporter_ip TEXT,
            reason TEXT,
            comment TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    logger.info('Database inizializzato con successo')

init_db()

waiting_users = []
active_rooms = {}
user_rooms = {}
user_data = {}
room_messages = {}

REPORT_THRESHOLD = 10
BAN_DURATION = 1800

VALID_REPORT_REASONS = [
    'inappropriate_language',
    'spam',
    'offensive_behavior',
    'threatening',
    'inappropriate_video',
    'other'
]

def get_db_connection():
    return sqlite3.connect(DB_PATH)

def jaccard_similarity(interests1: List[str], interests2: List[str]) -> float:
    if not interests1 or not interests2:
        return 0.0
    set1 = set(interests1)
    set2 = set(interests2)
    intersection = len(set1.intersection(set2))
    union = len(set1.union(set2))
    return intersection / union if union else 0.0

def get_sorted_waiting_partners(current_user_id: str, chat_mode: str) -> List[str]:
    current_interests = user_data.get(current_user_id, {}).get('interests', [])
    
    compatible_users = []
    for w_user_id in waiting_users:
        w_data = user_data.get(w_user_id, {})
        w_ip = w_data.get('ip')
        w_mode = w_data.get('chat_mode')
        
        # Solo utenti con lo stesso chat_mode
        if w_mode != chat_mode:
            continue
            
        if w_ip and is_banned(w_ip)[0]:
            continue
            
        w_interests = w_data.get('interests', [])
        sim = jaccard_similarity(current_interests, w_interests)
        compatible_users.append((sim, w_user_id))
    
    compatible_users.sort(key=lambda x: x[0], reverse=True)
    return [uid for _, uid in compatible_users]

def cleanup_expired_bans():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM user_bans WHERE ban_end < CURRENT_TIMESTAMP')
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    if deleted > 0:
        logger.info(f'{deleted} ban scaduti rimossi dal DB')
    threading.Timer(60.0, cleanup_expired_bans).start()

def cleanup_room_messages():
    now = time.time()
    max_age = 3600
    to_remove = []
    for room_id in room_messages:
        if room_id not in active_rooms:
            created_at = active_rooms.get(room_id, {}).get('created_at', now)
            if now - created_at > max_age:
                to_remove.append(room_id)
    
    for room_id in to_remove:
        del room_messages[room_id]
        logger.info(f'Messaggi della stanza {room_id} rimossi dalla memoria')
    
    threading.Timer(3600, cleanup_room_messages).start()

def is_banned(user_ip: str) -> tuple[bool, Optional[datetime]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT ban_end FROM user_bans WHERE ip = ?', (user_ip,))
    result = cursor.fetchone()
    conn.close()
    if result:
        ban_end = datetime.fromisoformat(result[0])
        if datetime.now() > ban_end:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('DELETE FROM user_bans WHERE ip = ?', (user_ip,))
            conn.commit()
            conn.close()
            logger.info(f'Ban scaduto rimosso per IP {user_ip}')
            return False, None
        return True, ban_end
    return False, None

def save_conversation_log(room_id: str, report_id: int, reported_ip: str, timestamp: str):
    if room_id not in room_messages:
        return
    
    log_filename = os.path.join(LOG_DIR, f'report_{report_id}_{timestamp.replace(":", "-")}.txt')
    try:
        with open(log_filename, 'w', encoding='utf-8') as f:
            f.write(f"Report ID: {report_id}\n")
            f.write(f"Reported IP: {reported_ip}\n")
            f.write(f"Timestamp: {timestamp}\n")
            f.write("Conversation Log:\n")
            f.write("-" * 50 + "\n")
            for msg in room_messages[room_id]:
                f.write(f"[{msg['timestamp']}] {msg['sender']}: {msg['message']}\n")
        logger.info(f'Log conversazione salvato: {log_filename}')
    except Exception as e:
        logger.error(f'Errore salvataggio log conversazione: {e}')

def ban_user(ip: str, reason='Multiple reports', sid_to_cleanup: Optional[str] = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT ban_count FROM user_bans WHERE ip = ?', (ip,))
    result = cursor.fetchone()
    ban_count = result[0] if result else 0
    duration = BAN_DURATION * (2 ** ban_count)
    ban_end = datetime.now() + timedelta(seconds=duration)
    new_ban_count = ban_count + 1
    cursor.execute('''
        INSERT OR REPLACE INTO user_bans (ip, ban_end, reason, ban_count)
        VALUES (?, ?, ?, ?)
    ''', (ip, ban_end.isoformat(), reason, new_ban_count))
    conn.commit()
    conn.close()
    logger.warning(f'IP {ip} bannato fino a {ban_end.strftime("%H:%M:%S")} per {reason} (ban #{new_ban_count}, durata: {duration//60} min)')
    
    if sid_to_cleanup:
        if sid_to_cleanup in waiting_users:
            waiting_users.remove(sid_to_cleanup)
        if sid_to_cleanup in user_rooms:
            room_id = user_rooms[sid_to_cleanup]
            if room_id in active_rooms:
                partner_id = next((uid for uid in active_rooms[room_id]['users'] if uid != sid_to_cleanup), None)
                if partner_id:
                    emit('partner_disconnected', room=partner_id)
                    if partner_id in user_rooms:
                        del user_rooms[partner_id]
                del active_rooms[room_id]
            del user_rooms[sid_to_cleanup]
        socketio.emit('force_disconnect', {'ban_end': ban_end.isoformat(), 'reason': reason}, room=sid_to_cleanup)
    
    cleanup_expired_bans()

def report_user(reporter_sid: str, reported_sid: str, reason: str, comment: str = ''):
    if reported_sid == reporter_sid:
        emit('error', {'message': 'Non puoi segnalare te stesso'}, room=reporter_sid)
        return
    
    reporter_ip = user_data.get(reporter_sid, {}).get('ip')
    reported_ip = user_data.get(reported_sid, {}).get('ip')
    if not reported_ip or not reporter_ip:
        emit('error', {'message': 'Utente non trovato'}, room=reporter_sid)
        return
    
    if reason not in VALID_REPORT_REASONS:
        emit('error', {'message': 'Motivo non valido'}, room=reporter_sid)
        return
    
    comment = html.escape(comment[:500])
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR IGNORE INTO user_reports (ip) VALUES (?)
    ''', (reported_ip,))
    cursor.execute('UPDATE user_reports SET report_count = report_count + 1, last_reported = CURRENT_TIMESTAMP WHERE ip = ?', (reported_ip,))
    cursor.execute('SELECT report_count FROM user_reports WHERE ip = ?', (reported_ip,))
    count = cursor.fetchone()[0]
    
    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    cursor.execute('''
        INSERT INTO report_log (reported_ip, reporter_ip, reason, comment)
        VALUES (?, ?, ?, ?)
    ''', (reported_ip, reporter_ip, reason, comment))
    report_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    room_id = user_rooms.get(reporter_sid)
    if room_id:
        save_conversation_log(room_id, report_id, reported_ip, timestamp)
    
    logger.info(f'Report da {reporter_sid} (IP: {reporter_ip}) su {reported_sid} (IP: {reported_ip}) per {reason}. Totale report: {count}')
    
    if count >= REPORT_THRESHOLD:
        ban_user(reported_ip, reason, sid_to_cleanup=reported_sid)

@app.route('/check_ban')
def check_ban():
    ip = request.remote_addr
    banned, ban_end = is_banned(ip)
    if banned and ban_end:
        return jsonify({
            'banned': True,
            'ban_end': ban_end.isoformat(),
            'reason': 'Previous ban active'
        })
    return jsonify({'banned': False})

@app.route('/terms')
def serve_terms():
    return send_from_directory('static', 'terms_of_use.pdf')

# Serve Privacy Policy PDF
@app.route('/privacy')
def serve_privacy():
    return send_from_directory('static', 'privacy_policy.pdf')

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="it" data-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ChatRoulette - Video & Testo</title>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        :root {
            --bg-primary: #1a1a1a;
            --bg-secondary: #2d2d2d;
            --bg-chat: #242424;
            --text-primary: #ffffff;
            --text-secondary: #adb5bd;
            --border-color: #404040;
            --accent: #4dabf7;
            --accent-hover: #339af0;
            --message-sent: #4dabf7;
            --message-received: #2d2d2d;
            --shadow: rgba(0,0,0,0.3);
            --success: #28a745;
            --danger: #dc3545;
            --warning: #ffc107;
        }

        [data-theme="light"] {
            --bg-primary: #ffffff;
            --bg-secondary: #f8f9fa;
            --bg-chat: #ffffff;
            --text-primary: #1a1a1a;
            --text-secondary: #6c757d;
            --border-color: #dee2e6;
            --accent: #0d6efd;
            --accent-hover: #0b5ed7;
            --message-sent: #0d6efd;
            --message-received: #e9ecef;
            --shadow: rgba(0,0,0,0.1);
            --success: #28a745;
            --danger: #dc3545;
            --warning: #ffc107;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            transition: background 0.3s, color 0.3s;
            overflow: hidden;
        }

        .mode-selector {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: linear-gradient(135deg, #1a1a1a 0%, #2d2d2d 100%);
            display: flex;
            justify-content: center;
            align-items: center;
            z-index: 2000;
            animation: fadeIn 0.5s ease-in-out;
        }

        .mode-selector.hidden {
            display: none;
        }

        .mode-card {
            background: var(--bg-secondary);
            border-radius: 24px;
            padding: 3rem;
            text-align: center;
            box-shadow: 0 12px 48px var(--shadow);
            max-width: 700px;
            width: 90%;
            position: relative;
            overflow: hidden;
            backdrop-filter: blur(10px);
            background: rgba(45, 45, 45, 0.85);
        }

        .mode-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: radial-gradient(circle at 50% 50%, rgba(77, 171, 247, 0.2), transparent 70%);
            z-index: -1;
        }

        .mode-card h1 {
            font-size: 3rem;
            font-weight: 700;
            margin-bottom: 1.5rem;
            background: linear-gradient(135deg, var(--accent), var(--success));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            animation: textGlow 2s ease-in-out infinite;
        }

        .mode-card p {
            color: var(--text-secondary);
            margin-bottom: 2rem;
            font-size: 1.2rem;
            line-height: 1.6;
        }

        .mode-options {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 2rem;
            margin-top: 2.5rem;
        }

        .mode-option {
            padding: 2rem;
            border: 3px solid var(--border-color);
            border-radius: 16px;
            cursor: pointer;
            transition: all 0.3s ease;
            background: var(--bg-primary);
            position: relative;
            overflow: hidden;
        }

        .mode-option:hover {
            border-color: var(--accent);
            transform: translateY(-8px);
            box-shadow: 0 12px 24px var(--shadow);
            background: linear-gradient(135deg, var(--accent), var(--bg-primary));
        }

        .mode-option-icon {
            font-size: 4rem;
            margin-bottom: 1.5rem;
            transition: transform 0.3s ease;
        }

        .mode-option:hover .mode-option-icon {
            transform: scale(1.2);
        }

        .mode-option-title {
            font-size: 1.5rem;
            font-weight: 600;
            margin-bottom: 0.8rem;
        }

        .mode-option-desc {
            font-size: 1rem;
            color: var(--text-secondary);
            line-height: 1.5;
        }

        .notice-section {
            margin-top: 2rem;
            padding: 1.5rem;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 12px;
            border: 1px solid var(--border-color);
            text-align: left;
        }

        .notice-section h3 {
            font-size: 1.3rem;
            margin-bottom: 1rem;
            color: var(--accent);
        }

        .notice-section p {
            font-size: 0.9rem;
            color: var(--text-secondary);
            margin-bottom: 1rem;
        }

        .notice-section a {
            color: var(--accent);
            text-decoration: none;
            font-weight: 600;
            transition: color 0.3s;
        }

        .notice-section a:hover {
            color: var(--accent-hover);
            text-decoration: underline;
        }

        @keyframes textGlow {
            0% { text-shadow: 0 0 10px var(--accent); }
            50% { text-shadow: 0 0 20px var(--accent), 0 0 30px var(--success); }
            100% { text-shadow: 0 0 10px var(--accent); }
        }

        /* Existing styles from the original code */
        .video-container {
            display: none;
            grid-template-columns: 1fr 1fr;
            gap: 1rem;
            padding: 1rem;
            background: #000;
            border-radius: 10px;
            margin-bottom: 1rem;
        }

        .video-container.active {
            display: grid;
        }

        .video-wrapper {
            position: relative;
            background: #1a1a1a;
            border-radius: 10px;
            overflow: hidden;
            aspect-ratio: 4/3;
        }

        .video-wrapper video {
            width: 100%;
            height: 100%;
            object-fit: cover;
        }

        .video-label {
            position: absolute;
            top: 10px;
            left: 10px;
            background: rgba(0, 0, 0, 0.7);
            color: white;
            padding: 0.5rem 1rem;
            border-radius: 5px;
            font-size: 0.9rem;
            font-weight: 600;
        }

        .video-controls {
            position: absolute;
            bottom: 10px;
            left: 50%;
            transform: translateX(-50%);
            display: flex;
            gap: 0.5rem;
        }

        .video-btn {
            background: rgba(0, 0, 0, 0.7);
            border: none;
            color: white;
            width: 40px;
            height: 40px;
            border-radius: 50%;
            cursor: pointer;
            font-size: 1.2rem;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.3s;
        }

        .video-btn:hover {
            background: rgba(0, 0, 0, 0.9);
            transform: scale(1.1);
        }

        .video-btn.active {
            background: var(--danger);
        }

        .ban-overlay {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.8);
            display: none;
            justify-content: center;
            align-items: center;
            z-index: 1000;
            animation: fadeIn 0.3s;
        }

        .ban-overlay.show {
            display: flex;
        }

        .ban-card {
            background: var(--bg-primary);
            border-radius: 20px;
            padding: 3rem;
            text-align: center;
            box-shadow: 0 10px 30px var(--shadow);
            max-width: 400px;
            color: var(--text-primary);
            border: 2px solid var(--danger);
        }

        .ban-card h2 {
            color: var(--danger);
            margin-bottom: 1rem;
            font-size: 1.5rem;
        }

        .ban-card p {
            margin-bottom: 2rem;
            color: var(--text-secondary);
        }

        .ban-timer {
            font-size: 2rem;
            font-weight: bold;
            color: var(--accent);
            margin-bottom: 1rem;
        }

        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }

        .report-modal {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.5);
            display: none;
            justify-content: center;
            align-items: center;
            z-index: 999;
            animation: fadeIn 0.3s;
        }

        .report-modal.show {
            display: flex;
        }

        .report-card {
            background: var(--bg-primary);
            border-radius: 20px;
            padding: 2rem;
            max-width: 400px;
            width: 90%;
            box-shadow: 0 10px 30px var(--shadow);
            color: var(--text-primary);
        }

        .report-card h2 {
            margin-bottom: 1rem;
            font-size: 1.3rem;
        }

        .report-card select,
        .report-card textarea {
            width: 100%;
            padding: 0.8rem;
            margin-bottom: 1rem;
            border: 2px solid var(--border-color);
            border-radius: 10px;
            background: var(--bg-primary);
            color: var(--text-primary);
            font-size: 0.9rem;
        }

        .report-card textarea {
            min-height: 100px;
            resize: vertical;
            maxlength: 500;
        }

        .report-card select:focus,
        .report-card textarea:focus {
            outline: none;
            border-color: var(--accent);
        }

        .report-actions {
            display: flex;
            gap: 1rem;
            justify-content: flex-end;
        }

        .header {
            background: var(--bg-secondary);
            padding: 1rem 2rem;
            box-shadow: 0 2px 10px var(--shadow);
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
        }

        .logo {
            font-size: 1.5rem;
            font-weight: 700;
            background: linear-gradient(135deg, var(--accent), var(--success));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }

        .header-controls {
            display: flex;
            gap: 1rem;
            align-items: center;
        }

        .theme-toggle {
            background: var(--bg-primary);
            border: 2px solid var(--border-color);
            border-radius: 50%;
            width: 40px;
            height: 40px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.2rem;
            transition: all 0.3s;
        }

        .theme-toggle:hover {
            transform: rotate(20deg);
            border-color: var(--accent);
        }

        .stats {
            display: flex;
            gap: 1rem;
        }

        .stat-item {
            background: var(--accent);
            color: white;
            padding: 0.5rem 1rem;
            border-radius: 20px;
            font-size: 0.9rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .stat-item.waiting {
            background: var(--warning);
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 2rem;
            display: grid;
            grid-template-columns: 300px 1fr;
            gap: 2rem;
            height: calc(100vh - 80px);
        }

        .sidebar {
            background: var(--bg-secondary);
            border-radius: 20px;
            padding: 2rem;
            box-shadow: 0 4px 20px var(--shadow);
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
            overflow-y: auto;
        }

        .sidebar h2 {
            font-size: 1.2rem;
            margin-bottom: 0.5rem;
        }

        .interest-tags {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
        }

        .tag {
            background: var(--border-color);
            color: var(--text-primary);
            padding: 0.4rem 0.8rem;
            border-radius: 15px;
            font-size: 0.85rem;
            cursor: pointer;
            transition: all 0.3s;
            border: 2px solid transparent;
        }

        .tag:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 8px var(--shadow);
        }

        .tag.active {
            background: var(--accent);
            color: white;
            border-color: var(--accent-hover);
        }

        .main-chat {
            background: var(--bg-chat);
            border-radius: 20px;
            box-shadow: 0 4px 20px var(--shadow);
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        .chat-header {
            background: var(--bg-secondary);
            padding: 1.5rem;
            border-bottom: 2px solid var(--border-color);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .status {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-weight: 600;
        }

        .status-indicator {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: var(--text-secondary);
            animation: pulse 2s infinite;
        }

        .status-indicator.connected {
            background: var(--success);
        }

        .status-indicator.searching {
            background: var(--warning);
        }

        .status-indicator.banned {
            background: var(--danger);
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        .chat-actions {
            display: flex;
            gap: 0.5rem;
        }

        .btn {
            padding: 0.6rem 1.5rem;
            border: none;
            border-radius: 10px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            font-size: 0.9rem;
        }

        .btn-primary {
            background: var(--accent);
            color: white;
        }

        .btn-primary:hover:not(:disabled) {
            background: var(--accent-hover);
            transform: translateY(-2px);
        }

        .btn-danger {
            background: var(--danger);
            color: white;
        }

        .btn-danger:hover:not(:disabled) {
            background: #c82333;
            transform: translateY(-2px);
        }

        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none !important;
        }

        .messages {
            flex: 1;
            padding: 2rem;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }

        .message {
            max-width: 70%;
            padding: 0.8rem 1.2rem;
            border-radius: 15px;
            word-wrap: break-word;
            animation: slideIn 0.3s;
        }

        @keyframes slideIn {
            from {
                opacity: 0;
                transform: translateY(10px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .message.sent {
            background: var(--message-sent);
            color: white;
            align-self: flex-end;
            border-bottom-right-radius: 5px;
        }

        .message.received {
            background: var(--message-received);
            color: var(--text-primary);
            align-self: flex-start;
            border-bottom-left-radius: 5px;
        }

        .message.system {
            background: transparent;
            color: var(--text-secondary);
            align-self: center;
            font-style: italic;
            font-size: 0.9rem;
            max-width: 100%;
            text-align: center;
        }

        .message-sender {
            font-weight: 600;
            font-size: 0.85rem;
            margin-bottom: 0.3rem;
            opacity: 0.8;
        }

        .message-time {
            font-size: 0.75rem;
            opacity: 0.7;
            margin-top: 0.3rem;
        }

        .input-area {
            padding: 1.5rem;
            background: var(--bg-secondary);
            border-top: 2px solid var(--border-color);
            display: flex;
            gap: 1rem;
        }

        #messageInput {
            flex: 1;
            padding: 0.8rem 1.2rem;
            border: 2px solid var(--border-color);
            border-radius: 10px;
            background: var(--bg-primary);
            color: var(--text-primary);
            font-size: 1rem;
            transition: all 0.3s;
        }

        #messageInput:focus {
            outline: none;
            border-color: var(--accent);
            box-shadow: 0 0 0 3px rgba(13, 110, 253, 0.1);
        }

        .typing-indicator {
            display: none;
            align-items: center;
            gap: 0.3rem;
            padding: 0.8rem 1.2rem;
            background: var(--message-received);
            border-radius: 15px;
            align-self: flex-start;
            max-width: 70px;
            margin-left: 2rem;
        }

        .typing-indicator.active {
            display: flex;
        }

        .typing-indicator span {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--text-secondary);
            animation: typing 1.4s infinite;
        }

        .typing-indicator span:nth-child(2) {
            animation-delay: 0.2s;
        }

        .typing-indicator span:nth-child(3) {
            animation-delay: 0.4s;
        }

        @keyframes typing {
            0%, 60%, 100% {
                transform: translateY(0);
            }
            30% {
                transform: translateY(-10px);
            }
        }

        .connection-status {
            position: fixed;
            bottom: 2rem;
            right: 2rem;
            padding: 1rem 1.5rem;
            border-radius: 10px;
            background: var(--bg-secondary);
            border: 2px solid var(--border-color);
            box-shadow: 0 4px 20px var(--shadow);
            display: none;
            align-items: center;
            gap: 0.5rem;
            animation: slideUp 0.3s;
        }

        .connection-status.show {
            display: flex;
        }

        @keyframes slideUp {
            from {
                opacity: 0;
                transform: translateY(20px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }

        .report-btn {
            background: var(--danger);
            color: white;
            border: none;
            padding: 0.5rem 1rem;
            border-radius: 5px;
            cursor: pointer;
            font-size: 0.8rem;
            margin-left: 0.5rem;
        }

        .report-btn:hover {
            background: #c82333;
        }

        .disabled-interface {
            pointer-events: none;
            opacity: 0.5;
        }

        @media (max-width: 968px) {
            .container {
                grid-template-columns: 1fr;
                gap: 1rem;
                padding: 1rem;
            }

            .sidebar {
                display: none;
            }

            .stats {
                flex-direction: column;
                gap: 0.5rem;
            }

            .mode-options {
                grid-template-columns: 1fr;
            }

            .video-container.active {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <!-- Mode Selector -->
    <div class="mode-selector" id="modeSelector">
        <div class="mode-card">
            <h1>🎭 ChatRoulette</h1>
            <p>Benvenuto! Scegli la tua modalità di chat preferita e connettiti con persone da tutto il mondo in modo anonimo e sicuro.</p>
            
            <div class="mode-options">
                <div class="mode-option" onclick="selectMode('text')">
                    <div class="mode-option-icon">💬</div>
                    <div class="mode-option-title">Solo Testo</div>
                    <div class="mode-option-desc">Chiacchiera in modo anonimo tramite messaggi di testo. Perfetto per conversazioni rapide e sicure.</div>
                </div>
                
                <div class="mode-option" onclick="selectMode('video')">
                    <div class="mode-option-icon">📹</div>
                    <div class="mode-option-title">Video + Testo</div>
                    <div class="mode-option-desc">Videochiamata con chat testuale integrata per un'esperienza più coinvolgente.</div>
                </div>
            </div>

            <div class="notice-section">
                <h3>Avviso Importante</h3>
                <p>ChatRoulette è una piattaforma per conversazioni anonime. Rispetta gli altri utenti e segui le nostre <a href="/terms" target="_blank">Condizioni d'Uso</a>. Le conversazioni sono monitorate per garantire un ambiente sicuro. L'uso improprio può portare a ban temporanei o permanenti.</p>
                <p>La tua privacy è importante: non condividiamo i tuoi dati personali. Per maggiori dettagli, consulta la nostra <a href="/privacy" target="_blank">Informativa sulla Privacy</a>.</p>
            </div>
        </div>
    </div>

    <!-- Ban Overlay -->
    <div class="ban-overlay" id="banOverlay">
        <div class="ban-card">
            <h2>🚫 Sei stato bannato</h2>
            <p>Attendi il termine del ban.</p>
            <div class="ban-timer" id="banTimer">30:00</div>
        </div>
    </div>

    <!-- Report Modal -->
    <div class="report-modal" id="reportModal">
        <div class="report-card">
            <h2>Segnala Utente</h2>
            <select id="reportReason">
                <option value="" disabled selected>Seleziona un motivo</option>
                <option value="inappropriate_language">Linguaggio inappropriato</option>
                <option value="spam">Spam</option>
                <option value="offensive_behavior">Comportamento offensivo</option>
                <option value="threatening">Comportamento minaccioso</option>
                <option value="inappropriate_video">Contenuto video inappropriato</option>
                <option value="other">Altro</option>
            </select>
            <textarea id="reportComment" placeholder="Aggiungi un commento (opzionale)" maxlength="500"></textarea>
            <div class="report-actions">
                <button class="btn btn-primary" onclick="submitReport()">Invia</button>
                <button class="btn btn-danger" onclick="closeReportModal()">Annulla</button>
            </div>
        </div>
    </div>

    <div class="header" id="header">
        <div class="logo">🎭 ChatRoulette</div>
        <div class="header-controls">
            <div class="stats">
                <div class="stat-item">
                    👥 <span id="onlineCount">0</span> online
                </div>
                <div class="stat-item waiting">
                    ⏳ <span id="waitingCount">0</span> in attesa
                </div>
            </div>
            <button class="theme-toggle" onclick="toggleTheme()">
                <span id="themeIcon">🌙</span>
            </button>
        </div>
    </div>

    <div class="container" id="container" style="display: none;">
        <div class="sidebar">
            <div>
                <h2>ℹ️ Informazioni</h2>
                <p style="color: var(--text-secondary); font-size: 0.9rem; margin-bottom: 1rem;">
                    Le conversazioni sono anonime: sei "Tu" e il tuo partner è "Stranger".
                </p>
                <p style="color: var(--text-secondary); font-size: 0.9rem;">
                    <strong>Modalità:</strong> <span id="currentMode">-</span>
                </p>
            </div>

            <div>
                <h2>🎯 Interessi</h2>
                <p style="color: var(--text-secondary); font-size: 0.9rem; margin-bottom: 1rem;">
                    Seleziona i tuoi interessi per trovare persone simili
                </p>
                <div class="interest-tags">
                    <div class="tag" onclick="toggleTag(this)">🎮 Gaming</div>
                    <div class="tag" onclick="toggleTag(this)">🎵 Musica</div>
                    <div class="tag" onclick="toggleTag(this)">🎬 Film</div>
                    <div class="tag" onclick="toggleTag(this)">📚 Libri</div>
                    <div class="tag" onclick="toggleTag(this)">⚽ Sport</div>
                    <div class="tag" onclick="toggleTag(this)">🎨 Arte</div>
                    <div class="tag" onclick="toggleTag(this)">💻 Tech</div>
                    <div class="tag" onclick="toggleTag(this)">🌍 Viaggi</div>
                    <div class="tag" onclick="toggleTag(this)">🍕 Cucina</div>
                </div>
            </div>

            <div>
                <h2>⚙️ Impostazioni</h2>
                <label style="display: flex; align-items: center; gap: 0.5rem; cursor: pointer; margin-top: 1rem;">
                    <input type="checkbox" id="soundToggle" checked style="width: 18px; height: 18px; cursor: pointer;">
                    <span>Suoni notifica</span>
                </label>
                <label style="display: flex; align-items: center; gap: 0.5rem; cursor: pointer; margin-top: 0.8rem;">
                    <input type="checkbox" id="timestampToggle" checked style="width: 18px; height: 18px; cursor: pointer;">
                    <span>Mostra timestamp</span>
                </label>
            </div>

            <div style="margin-top: auto; padding-top: 1rem; border-top: 2px solid var(--border-color);">
                <button class="btn btn-danger" style="width: 100%;" onclick="changeMode()">
                    🔄 Cambia Modalità
                </button>
            </div>
        </div>

        <div class="main-chat">
            <div class="chat-header">
                <div class="status">
                    <div class="status-indicator" id="statusIndicator"></div>
                    <span id="statusText">Disconnesso</span>
                </div>
                <div class="chat-actions">
                    <button class="btn btn-primary" id="startBtn" onclick="startChat()">
                        🚀 Inizia Chat
                    </button>
                    <button class="btn btn-danger" id="stopBtn" onclick="stopChat()" style="display: none;">
                        ⏹️ Ferma
                    </button>
                    <button class="btn btn-danger" id="nextBtn" onclick="nextChat()" disabled>
                        ⏭️ Prossimo
                    </button>
                    <button class="report-btn" id="reportBtn" onclick="openReportModal()" style="display: none;" title="Segnala utente">
                        🚨 Report
                    </button>
                </div>
            </div>

            <div class="video-container" id="videoContainer">
                <div class="video-wrapper">
                    <video id="localVideo" autoplay muted playsinline></video>
                    <div class="video-label">Tu</div>
                    <div class="video-controls">
                        <button class="video-btn" id="toggleVideoBtn" onclick="toggleVideo()" title="Attiva/Disattiva video">
                            📹
                        </button>
                        <button class="video-btn" id="toggleAudioBtn" onclick="toggleAudio()" title="Attiva/Disattiva audio">
                            🎤
                        </button>
                    </div>
                </div>
                <div class="video-wrapper">
                    <video id="remoteVideo" autoplay playsinline></video>
                    <div class="video-label">Stranger</div>
                </div>
            </div>

            <div class="messages" id="messages"></div>

            <div class="typing-indicator" id="typingIndicator">
                <span></span>
                <span></span>
                <span></span>
            </div>

            <div class="input-area">
                <input 
                    type="text" 
                    id="messageInput" 
                    placeholder="Scrivi un messaggio..." 
                    disabled
                    onkeypress="handleKeyPress(event)"
                    oninput="handleTyping()"
                >
                <button class="btn btn-primary" onclick="sendMessage()" id="sendBtn" disabled>
                    📤 Invia
                </button>
            </div>
        </div>
    </div>

    <div class="connection-status" id="connectionStatus">
        <div class="status-indicator connected"></div>
        <span>Connesso al server</span>
    </div>

    <script>
        let socket;
        let isConnected = false;
        let isSearching = false;
        let isTyping = false;
        let typingTimeout;
        let reconnectAttempts = 0;
        const MAX_RECONNECT_ATTEMPTS = 5;
        let currentPartnerId = null;
        let isBanned = false;
        let banInterval;
        let chatMode = null;
        
        let localStream = null;
        let peerConnection = null;
        let isVideoEnabled = true;
        let isAudioEnabled = true;
        
        const ICE_SERVERS = {
            iceServers: [
                { urls: 'stun:stun.l.google.com:19302' },
                { urls: 'stun:stun1.l.google.com:19302' }
            ]
        };

        function selectMode(mode) {
            chatMode = mode;
            document.getElementById('modeSelector').classList.add('hidden');
            document.getElementById('container').style.display = 'grid';
            document.getElementById('currentMode').textContent = mode === 'video' ? '📹 Video + Testo' : '💬 Solo Testo';
            
            if (mode === 'video') {
                initializeMedia();
            }
            
            checkBanStatus();
        }

        function changeMode() {
            if (isConnected || isSearching) {
                if (!confirm('Sei sicuro di voler cambiare modalità? La chat corrente verrà terminata.')) {
                    return;
                }
                if (isConnected) {
                    nextChat();
                } else if (isSearching) {
                    stopChat();
                }
            }
            
            if (socket && socket.connected && !isBanned) {
                socket.emit('change_mode');
                socket.disconnect();
            }
            
            if (localStream) {
                localStream.getTracks().forEach(track => track.stop());
                localStream = null;
            }
            
            if (peerConnection) {
                peerConnection.close();
                peerConnection = null;
            }
            
            chatMode = null;
            document.getElementById('container').style.display = 'none';
            document.getElementById('modeSelector').classList.remove('hidden');
            document.getElementById('videoContainer').classList.remove('active');
            document.getElementById('messages').innerHTML = '';
        }

        async function initializeMedia() {
            try {
                localStream = await navigator.mediaDevices.getUserMedia({
                    video: true,
                    audio: true
                });
                
                document.getElementById('localVideo').srcObject = localStream;
                document.getElementById('videoContainer').classList.add('active');
                addSystemMessage('✅ Camera e microfono attivati');
            } catch (error) {
                console.error('Errore accesso media:', error);
                addSystemMessage('❌ Impossibile accedere a camera/microfono. Assicurati di aver dato i permessi.');
                chatMode = 'text';
                document.getElementById('currentMode').textContent = '💬 Solo Testo (fallback)';
            }
        }

        function toggleVideo() {
            if (!localStream) return;
            
            isVideoEnabled = !isVideoEnabled;
            localStream.getVideoTracks().forEach(track => {
                track.enabled = isVideoEnabled;
            });
            
            const btn = document.getElementById('toggleVideoBtn');
            btn.classList.toggle('active', !isVideoEnabled);
            btn.textContent = isVideoEnabled ? '📹' : '📹';
            btn.style.background = isVideoEnabled ? 'rgba(0, 0, 0, 0.7)' : 'var(--danger)';
        }

        function toggleAudio() {
            if (!localStream) return;
            
            isAudioEnabled = !isAudioEnabled;
            localStream.getAudioTracks().forEach(track => {
                track.enabled = isAudioEnabled;
            });
            
            const btn = document.getElementById('toggleAudioBtn');
            btn.classList.toggle('active', !isAudioEnabled);
            btn.textContent = isAudioEnabled ? '🎤' : '🔇';
            btn.style.background = isAudioEnabled ? 'rgba(0, 0, 0, 0.7)' : 'var(--danger)';
        }

        async function createPeerConnection() {
            peerConnection = new RTCPeerConnection(ICE_SERVERS);
            
            if (localStream) {
                localStream.getTracks().forEach(track => {
                    peerConnection.addTrack(track, localStream);
                });
            }
            
            peerConnection.ontrack = (event) => {
                document.getElementById('remoteVideo').srcObject = event.streams[0];
            };
            
            peerConnection.onicecandidate = (event) => {
                if (event.candidate) {
                    socket.emit('ice_candidate', {
                        candidate: event.candidate
                    });
                }
            };
            
            peerConnection.onconnectionstatechange = () => {
                console.log('Connection state:', peerConnection.connectionState);
                if (peerConnection.connectionState === 'disconnected' || 
                    peerConnection.connectionState === 'failed') {
                    addSystemMessage('⚠️ Connessione video persa');
                }
            };
        }

        async function createOffer() {
            await createPeerConnection();
            const offer = await peerConnection.createOffer();
            await peerConnection.setLocalDescription(offer);
            socket.emit('video_offer', { offer: offer });
        }

        async function handleOffer(offer) {
            await createPeerConnection();
            await peerConnection.setRemoteDescription(new RTCSessionDescription(offer));
            const answer = await peerConnection.createAnswer();
            await peerConnection.setLocalDescription(answer);
            socket.emit('video_answer', { answer: answer });
        }

        async function handleAnswer(answer) {
            await peerConnection.setRemoteDescription(new RTCSessionDescription(answer));
        }

        async function handleIceCandidate(candidate) {
            if (peerConnection) {
                await peerConnection.addIceCandidate(new RTCIceCandidate(candidate));
            }
        }

        async function checkBanStatus() {
            try {
                const response = await fetch('/check_ban');
                const data = await response.json();
                if (data.banned) {
                    isBanned = true;
                    showBanOverlay(data.reason, data.ban_end);
                    disableInterface();
                } else {
                    hideBanOverlay();
                    enableInterface();
                    initSocket();
                }
            } catch (error) {
                console.error('Errore verifica ban:', error);
                initSocket();
            }
        }

        function initSocket() {
            socket = io({
                reconnection: false,
                reconnectionDelay: 1000,
                reconnectionDelayMax: 5000,
                reconnectionAttempts: MAX_RECONNECT_ATTEMPTS
            });

            setupSocketListeners();
        }

        function setupSocketListeners() {
            socket.on('connect', () => {
                console.log('✅ Connesso al server');
                reconnectAttempts = 0;
                isBanned = false;
                showConnectionStatus('Connesso al server', true);
                updateStatus('Connesso al server', false);
                hideBanOverlay();
                enableInterface();
                setTimeout(() => socket.emit('get_stats'), 100);
            });

            socket.on('disconnect', () => {
                console.log('❌ Disconnesso dal server');
                isConnected = false;
                isSearching = false;
                showConnectionStatus('Disconnesso dal server', false);
                updateStatus('Disconnesso', false);
                disableChat();
            });

            socket.on('connect_error', (error) => {
                console.error('Errore connessione:', error);
                reconnectAttempts++;
                if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
                    showConnectionStatus('Impossibile connettersi al server', false);
                }
            });

            socket.on('banned', (data) => {
                console.log('🚫 Utente bannato:', data);
                isBanned = true;
                showBanOverlay(data.reason, data.ban_end);
                disableInterface();
                socket.disconnect();
            });

            socket.on('force_disconnect', (data) => {
                console.log('🚫 Disconnessione forzata (ban)');
                isBanned = true;
                showBanOverlay(data.reason, data.ban_end);
                disableInterface();
                socket.disconnect();
            });

            socket.on('stats_update', (data) => {
                console.log('📊 Stats:', data);
                document.getElementById('onlineCount').textContent = data.online || 0;
                document.getElementById('waitingCount').textContent = data.waiting || 0;
            });

            socket.on('waiting', () => {
                if (isBanned) return;
                console.log('⏳ In attesa di un partner...');
                isSearching = true;
                updateStatus('Cercando un partner compatibile...', false, true);
                addSystemMessage('🔍 Ricerca di un partner in corso... (modalità: ' + (chatMode === 'video' ? 'video' : 'testo') + ')');
                document.getElementById('stopBtn').style.display = 'inline-block';
                document.getElementById('startBtn').style.display = 'none';
            });

            socket.on('matched', async (data) => {
                if (isBanned) return;
                console.log('✅ Match trovato!', data);
                isConnected = true;
                isSearching = false;
                currentPartnerId = data.partner_id;
                updateStatus('Connesso con Stranger', true);
                addSystemMessage('✅ Connesso con Stranger! Inizia a chattare!');
                enableChat();
                document.getElementById('reportBtn').style.display = 'inline-block';
                playSound('connect');
                
                document.getElementById('stopBtn').style.display = 'none';
                document.getElementById('startBtn').style.display = 'none';
                
                if (chatMode === 'video' && data.initiator) {
                    await createOffer();
                }
            });

            socket.on('video_offer', async (data) => {
                if (chatMode === 'video') {
                    await handleOffer(data.offer);
                }
            });

            socket.on('video_answer', async (data) => {
                if (chatMode === 'video') {
                    await handleAnswer(data.answer);
                }
            });

            socket.on('ice_candidate', async (data) => {
                if (chatMode === 'video') {
                    await handleIceCandidate(data.candidate);
                }
            });

            socket.on('message', (data) => {
                if (isBanned) return;
                console.log('📨 Messaggio ricevuto:', data);
                addMessage(data.message, 'received');
                playSound('message');
            });

            socket.on('partner_disconnected', () => {
                if (isBanned) return;
                console.log('👋 Partner disconnesso');
                isConnected = false;
                isSearching = false;
                currentPartnerId = null;
                updateStatus('Partner disconnesso', false);
                addSystemMessage('❌ Il tuo partner si è disconnesso');
                disableChat();
                document.getElementById('reportBtn').style.display = 'none';
                playSound('disconnect');
                
                if (peerConnection) {
                    peerConnection.close();
                    peerConnection = null;
                }
                document.getElementById('remoteVideo').srcObject = null;
                
                document.getElementById('stopBtn').style.display = 'none';
                document.getElementById('startBtn').style.display = 'inline-block';
            });

            socket.on('typing', () => {
                if (isBanned) return;
                document.getElementById('typingIndicator').classList.add('active');
                scrollToBottom();
            });

            socket.on('stop_typing', () => {
                document.getElementById('typingIndicator').classList.remove('active');
            });

            socket.on('error', (data) => {
                console.error('❌ Errore:', data.message);
                addSystemMessage('❌ Errore: ' + data.message);
            });
        }

        function showBanOverlay(reason, banEndIso) {
            const overlay = document.getElementById('banOverlay');
            const timerEl = document.getElementById('banTimer');
            
            const banEnd = new Date(banEndIso);
            updateBanTimer(banEnd);
            
            banInterval = setInterval(() => {
                updateBanTimer(banEnd);
            }, 1000);
            
            overlay.classList.add('show');
            disableInterface();
        }

        function hideBanOverlay() {
            const overlay = document.getElementById('banOverlay');
            overlay.classList.remove('show');
            if (banInterval) {
                clearInterval(banInterval);
                banInterval = null;
            }
            enableInterface();
        }

        function updateBanTimer(banEnd) {
            const now = new Date();
            const diff = banEnd - now;
            if (diff <= 0) {
                document.getElementById('banTimer').textContent = '00:00';
                clearInterval(banInterval);
                isBanned = false;
                hideBanOverlay();
                addSystemMessage('✅ Ban terminato. Puoi tornare a chattare.');
                checkBanStatus();
                return;
            }
            
            const minutes = Math.floor(diff / 60000);
            const seconds = Math.floor((diff % 60000) / 1000);
            document.getElementById('banTimer').textContent = `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
        }

        function openReportModal() {
            if (isBanned || !currentPartnerId) return;
            document.getElementById('reportModal').classList.add('show');
            document.getElementById('reportReason').value = '';
            document.getElementById('reportComment').value = '';
        }

        function closeReportModal() {
            document.getElementById('reportModal').classList.remove('show');
        }

        function submitReport() {
            if (isBanned || !currentPartnerId) return;
            const reason = document.getElementById('reportReason').value;
            const comment = document.getElementById('reportComment').value.trim();
            
            if (!reason) {
                alert('Seleziona un motivo per la segnalazione');
                return;
            }
            
            socket.emit('report_user', {
                reported_id: currentPartnerId,
                reason: reason,
                comment: comment
            });
            closeReportModal();
            addSystemMessage('✅ Segnalazione inviata');
        }

        function disableInterface() {
            document.getElementById('container').classList.add('disabled-interface');
            document.getElementById('header').classList.add('disabled-interface');
            disableChat();
        }

        function enableInterface() {
            document.getElementById('container').classList.remove('disabled-interface');
            document.getElementById('header').classList.remove('disabled-interface');
        }

        function toggleTheme() {
            if (isBanned) return;
            const html = document.documentElement;
            const icon = document.getElementById('themeIcon');
            const currentTheme = html.getAttribute('data-theme');

            if (currentTheme === 'light') {
                html.setAttribute('data-theme', 'dark');
                icon.textContent = '☀️';
            } else {
                html.setAttribute('data-theme', 'light');
                icon.textContent = '🌙';
            }
        }

        function toggleTag(element) {
            if (isBanned) return;
            element.classList.toggle('active');
        }

        function getSelectedInterests() {
            const tags = document.querySelectorAll('.tag.active');
            return Array.from(tags).map(tag => tag.textContent.trim());
        }

        function startChat() {
            if (!socket || !socket.connected || isBanned) {
                if (isBanned) {
                    return;
                } else {
                    addSystemMessage('❌ Connessione al server non disponibile. Riprova tra poco.');
                }
                return;
            }

            const interests = getSelectedInterests();
            
            const messages = document.getElementById('messages');
            messages.innerHTML = '';
            
            socket.emit('find_partner', { 
                interests: interests,
                chat_mode: chatMode
            });
            
            document.getElementById('startBtn').disabled = true;
        }

        function stopChat() {
            if (isSearching && !isBanned) {
                socket.emit('stop_searching');
                isSearching = false;
                updateStatus('Connesso al server', false);
                addSystemMessage('⏹️ Ricerca interrotta');
                
                document.getElementById('stopBtn').style.display = 'none';
                document.getElementById('startBtn').style.display = 'inline-block';
                document.getElementById('startBtn').disabled = false;
            }
        }

        function nextChat() {
            if (isBanned) return;
            socket.emit('next_partner');
            isConnected = false;
            isSearching = false;
            currentPartnerId = null;
            
            const messages = document.getElementById('messages');
            messages.innerHTML = '';
            
            disableChat();
            document.getElementById('reportBtn').style.display = 'none';
            
            if (peerConnection) {
                peerConnection.close();
                peerConnection = null;
            }
            document.getElementById('remoteVideo').srcObject = null;
            
            document.getElementById('stopBtn').style.display = 'none';
            document.getElementById('startBtn').style.display = 'inline-block';
            document.getElementById('startBtn').disabled = false;
            
            updateStatus('Connesso al server', false);
        }

        function enableChat() {
            if (isBanned) return;
            document.getElementById('messageInput').disabled = false;
            document.getElementById('sendBtn').disabled = false;
            document.getElementById('nextBtn').disabled = false;
            document.getElementById('messageInput').focus();
        }

        function disableChat() {
            document.getElementById('messageInput').disabled = true;
            document.getElementById('sendBtn').disabled = true;
            document.getElementById('nextBtn').disabled = true;
            document.getElementById('startBtn').disabled = isBanned;
            document.getElementById('reportBtn').style.display = 'none';
        }

        function sendMessage() {
            if (isBanned || !isConnected) return;
            const input = document.getElementById('messageInput');
            const message = input.value.trim();
            
            if (message) {
                socket.emit('send_message', { message: message });
                addMessage(message, 'sent');
                input.value = '';
                playSound('send');
                socket.emit('stop_typing');
                isTyping = false;
            }
        }

        function handleKeyPress(event) {
            if (isBanned) return;
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                sendMessage();
            }
        }

        function handleTyping() {
            if (!isConnected || isBanned) return;
            
            const input = document.getElementById('messageInput');
            if (input.value.trim() === '') {
                if (isTyping) {
                    isTyping = false;
                    socket.emit('stop_typing');
                }
                return;
            }

            if (!isTyping) {
                isTyping = true;
                socket.emit('typing');
            }

            clearTimeout(typingTimeout);
            typingTimeout = setTimeout(() => {
                isTyping = false;
                socket.emit('stop_typing');
            }, 1000);
        }

        function addMessage(text, type) {
            if (isBanned) return;
            const messages = document.getElementById('messages');
            const messageDiv = document.createElement('div');
            messageDiv.className = `message ${type}`;
            
            const showTimestamp = document.getElementById('timestampToggle').checked;
            const time = new Date().toLocaleTimeString('it-IT', { 
                hour: '2-digit', 
                minute: '2-digit' 
            });
            
            let content = '';
            if (type === 'received') {
                content += `<div class="message-sender">Stranger</div>`;
            } else if (type === 'sent') {
                content += `<div class="message-sender">Tu</div>`;
            }
            content += text;
            if (showTimestamp && type !== 'system') {
                content += `<div class="message-time">${time}</div>`;
            }
            
            messageDiv.innerHTML = content;
            messages.appendChild(messageDiv);
            scrollToBottom();
        }

        function addSystemMessage(text) {
            const messages = document.getElementById('messages');
            const messageDiv = document.createElement('div');
            messageDiv.className = 'message system';
            messageDiv.textContent = text;
            messages.appendChild(messageDiv);
            scrollToBottom();
        }

        function updateStatus(text, connected, searching = false, banned = false) {
            document.getElementById('statusText').textContent = text;
            const indicator = document.getElementById('statusIndicator');
            indicator.classList.remove('connected', 'searching', 'banned');
            
            if (banned) {
                indicator.classList.add('banned');
            } else if (connected) {
                indicator.classList.add('connected');
            } else if (searching) {
                indicator.classList.add('searching');
            }
        }

        function showConnectionStatus(text, isConnected) {
            if (isBanned) return;
            const status = document.getElementById('connectionStatus');
            const indicator = status.querySelector('.status-indicator');
            const span = status.querySelector('span');
            
            span.textContent = text;
            
            if (isConnected) {
                indicator.classList.add('connected');
            } else {
                indicator.classList.remove('connected');
            }
            
            status.classList.add('show');
            
            setTimeout(() => {
                status.classList.remove('show');
            }, 3000);
        }

        function scrollToBottom() {
            const messages = document.getElementById('messages');
            messages.scrollTop = messages.scrollHeight;
        }

        function playSound(type) {
            if (!document.getElementById('soundToggle').checked || isBanned) return;
            
            try {
                const audioContext = new (window.AudioContext || window.webkitAudioContext)();
                const oscillator = audioContext.createOscillator();
                const gainNode = audioContext.createGain();
                
                oscillator.connect(gainNode);
                gainNode.connect(audioContext.destination);
                
                switch(type) {
                    case 'message':
                        oscillator.frequency.value = 800;
                        gainNode.gain.setValueAtTime(0.1, audioContext.currentTime);
                        gainNode.gain.exponentialRampToValueAtTime(0.01, audioContext.currentTime + 0.1);
                        oscillator.start(audioContext.currentTime);
                        oscillator.stop(audioContext.currentTime + 0.1);
                        break;
                    case 'send':
                        oscillator.frequency.value = 600;
                        gainNode.gain.setValueAtTime(0.05, audioContext.currentTime);
                        gainNode.gain.exponentialRampToValueAtTime(0.01, audioContext.currentTime + 0.08);
                        oscillator.start(audioContext.currentTime);
                        oscillator.stop(audioContext.currentTime + 0.08);
                        break;
                    case 'connect':
                        oscillator.frequency.value = 800;
                        gainNode.gain.setValueAtTime(0.1, audioContext.currentTime);
                        gainNode.gain.exponentialRampToValueAtTime(0.01, audioContext.currentTime + 0.1);
                        oscillator.start(audioContext.currentTime);
                        oscillator.stop(audioContext.currentTime + 0.1);
                        
                        setTimeout(() => {
                            const osc2 = audioContext.createOscillator();
                            const gain2 = audioContext.createGain();
                            osc2.connect(gain2);
                            gain2.connect(audioContext.destination);
                            osc2.frequency.value = 1000;
                            gain2.gain.setValueAtTime(0.1, audioContext.currentTime);
                            gain2.gain.exponentialRampToValueAtTime(0.01, audioContext.currentTime + 0.1);
                            osc2.start(audioContext.currentTime);
                            osc2.stop(audioContext.currentTime + 0.1);
                        }, 100);
                        break;
                    case 'disconnect':
                        oscillator.frequency.value = 400;
                        gainNode.gain.setValueAtTime(0.1, audioContext.currentTime);
                        gainNode.gain.exponentialRampToValueAtTime(0.01, audioContext.currentTime + 0.2);
                        oscillator.start(audioContext.currentTime);
                        oscillator.stop(audioContext.currentTime + 0.2);
                        break;
                }
            } catch (e) {
                console.warn('Audio non disponibile:', e);
            }
        }

        window.addEventListener('beforeunload', () => {
            if (socket && socket.connected && !isBanned) {
                socket.disconnect();
            }
            if (localStream) {
                localStream.getTracks().forEach(track => track.stop());
            }
            if (peerConnection) {
                peerConnection.close();
            }
        });
    </script>
</body>
</html>
'''

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@socketio.on('change_mode')
def handle_change_mode():
    user_id = request.sid
    logger.info(f'🔄 Utente {user_id} cambia modalità, pulizia sessione')
    
    # Rimuovi utente da waiting_users, user_rooms, e active_rooms
    if user_id in waiting_users:
        waiting_users.remove(user_id)
        logger.info(f'Rimosso {user_id} dalla lista di attesa')
    
    if user_id in user_rooms:
        room_id = user_rooms[user_id]
        if room_id in active_rooms:
            partner_id = next((uid for uid in active_rooms[room_id]['users'] if uid != user_id), None)
            if partner_id:
                socketio.emit('partner_disconnected', room=partner_id)
                if partner_id in user_rooms:
                    del user_rooms[partner_id]
            del active_rooms[room_id]
            logger.info(f'Stanza {room_id} eliminata per cambio modalità')
        del user_rooms[user_id]
    
    # Rimuovi dati utente
    if user_id in user_data:
        del user_data[user_id]
        logger.info(f'Dati utente {user_id} rimossi')
    
    # Aggiorna statistiche
    emit_stats()

@socketio.on('connect')
def handle_connect(auth):
    user_id = request.sid
    ip = request.remote_addr
    banned, ban_end = is_banned(ip)
    if banned:
        emit('banned', {'duration': f'{BAN_DURATION // 60} minuti', 'ban_end': ban_end.isoformat() if ban_end else None})
        return False
    
    user_data[user_id] = {
        'connected_at': time.time(),
        'room': None,
        'interests': [],
        'ip': ip,
        'chat_mode': None
    }
    logger.info(f'✅ Utente connesso: {user_id} (IP: {ip})')
    emit_stats()

@socketio.on('disconnect')
def handle_disconnect():
    user_id = request.sid
    logger.info(f'❌ Utente disconnesso: {user_id}')
    
    if user_id in waiting_users:
        waiting_users.remove(user_id)
        logger.info(f'Rimosso dalla lista di attesa: {user_id}')
    
    if user_id in user_rooms:
        room_id = user_rooms[user_id]
        if room_id in active_rooms:
            partner_id = next((uid for uid in active_rooms[room_id]['users'] if uid != user_id), None)
            if partner_id:
                logger.info(f'Notifico partner {partner_id} della disconnessione')
                socketio.emit('partner_disconnected', room=partner_id)
                if partner_id in user_rooms:
                    del user_rooms[partner_id]
            del active_rooms[room_id]
            logger.info(f'Stanza eliminata: {room_id}')
        
        del user_rooms[user_id]
    
    if user_id in user_data:
        del user_data[user_id]
    
    emit_stats()

def cleanup_stale_sessions():
    now = time.time()
    timeout = 300  # 5 minuti
    to_remove = []
    
    for user_id, data in user_data.items():
        if now - data['connected_at'] > timeout:
            to_remove.append(user_id)
    
    for user_id in to_remove:
        if user_id in waiting_users:
            waiting_users.remove(user_id)
        if user_id in user_rooms:
            room_id = user_rooms[user_id]
            if room_id in active_rooms:
                partner_id = next((uid for uid in active_rooms[room_id]['users'] if uid != user_id), None)
                if partner_id:
                    socketio.emit('partner_disconnected', room=partner_id)
                    if partner_id in user_rooms:
                        del user_rooms[partner_id]
                del active_rooms[room_id]
            del user_rooms[user_id]
        del user_data[user_id]
        logger.info(f'Sessione stale {user_id} rimossa')
    
    if to_remove:
        emit_stats()
    
    threading.Timer(60.0, cleanup_stale_sessions).start()

# Avvia cleanup all'avvio del server
cleanup_stale_sessions()

@socketio.on('get_stats')
def handle_get_stats():
    emit_stats(room=request.sid)

@socketio.on('find_partner')
def handle_find_partner(data):
    user_id = request.sid
    user_ip = user_data.get(user_id, {}).get('ip')
    if not user_ip:
        return
    banned, _ = is_banned(user_ip)
    if banned:
        emit('banned', {'duration': f'{BAN_DURATION // 60} minuti'})
        return
    
    chat_mode = data.get('chat_mode', 'text')
    logger.info(f'🔍 {user_id} cerca partner (mode: {chat_mode})')
    
    if user_id in user_data:
        user_data[user_id]['interests'] = data.get('interests', [])
        user_data[user_id]['chat_mode'] = chat_mode
    
    if user_id in waiting_users:
        waiting_users.remove(user_id)
    
    sorted_waiting = get_sorted_waiting_partners(user_id, chat_mode)
    partner_id = None
    
    for w_id in sorted_waiting:
        if w_id != user_id and w_id in user_data:
            p_ip = user_data[w_id]['ip']
            p_banned, _ = is_banned(p_ip)
            if not p_banned:
                partner_id = w_id
                break
    
    if partner_id:
        waiting_users.remove(partner_id)
        
        room_id = secrets.token_hex(8)
        active_rooms[room_id] = {
            'users': [user_id, partner_id],
            'created_at': time.time(),
            'chat_mode': chat_mode
        }
        room_messages[room_id] = []
        
        user_rooms[user_id] = room_id
        user_rooms[partner_id] = room_id
        
        join_room(room_id, sid=user_id)
        join_room(room_id, sid=partner_id)
        
        # Il primo utente è l'initiator per WebRTC
        emit('matched', {'room': room_id, 'partner_name': 'Stranger', 'partner_id': partner_id, 'initiator': True}, room=user_id)
        emit('matched', {'room': room_id, 'partner_name': 'Stranger', 'partner_id': user_id, 'initiator': False}, room=partner_id)
        
        logger.info(f'✅ Match creato: {user_id} <-> {partner_id} in stanza {room_id} (mode: {chat_mode})')
        emit_stats()
    else:
        waiting_users.append(user_id)
        logger.info(f'⏳ {user_id} aggiunto alla lista di attesa (mode: {chat_mode})')
        emit('waiting')
        emit_stats()

@socketio.on('video_offer')
def handle_video_offer(data):
    user_id = request.sid
    if user_id in user_rooms:
        room_id = user_rooms[user_id]
        if room_id in active_rooms:
            partner_id = next((uid for uid in active_rooms[room_id]['users'] if uid != user_id), None)
            if partner_id:
                emit('video_offer', {'offer': data['offer']}, room=partner_id)

@socketio.on('video_answer')
def handle_video_answer(data):
    user_id = request.sid
    if user_id in user_rooms:
        room_id = user_rooms[user_id]
        if room_id in active_rooms:
            partner_id = next((uid for uid in active_rooms[room_id]['users'] if uid != user_id), None)
            if partner_id:
                emit('video_answer', {'answer': data['answer']}, room=partner_id)

@socketio.on('ice_candidate')
def handle_ice_candidate(data):
    user_id = request.sid
    if user_id in user_rooms:
        room_id = user_rooms[user_id]
        if room_id in active_rooms:
            partner_id = next((uid for uid in active_rooms[room_id]['users'] if uid != user_id), None)
            if partner_id:
                emit('ice_candidate', {'candidate': data['candidate']}, room=partner_id)

@socketio.on('stop_searching')
def handle_stop_searching():
    user_id = request.sid
    user_ip = user_data.get(user_id, {}).get('ip')
    if not user_ip:
        return
    banned, _ = is_banned(user_ip)
    if banned:
        return
    if user_id in waiting_users:
        waiting_users.remove(user_id)
        logger.info(f'⏹️ {user_id} ha fermato la ricerca')
        emit_stats()

@socketio.on('send_message')
def handle_message(data):
    user_id = request.sid
    user_ip = user_data.get(user_id, {}).get('ip')
    if not user_ip:
        return
    banned, _ = is_banned(user_ip)
    if banned:
        return
    
    if user_id not in user_rooms:
        emit('error', {'message': 'Non sei connesso a nessuna chat'})
        return
    
    room_id = user_rooms[user_id]
    if room_id not in active_rooms:
        emit('error', {'message': 'La stanza non esiste più'})
        return
    
    partner_id = next((uid for uid in active_rooms[room_id]['users'] if uid != user_id), None)
    if partner_id:
        p_ip = user_data.get(partner_id, {}).get('ip')
        p_banned, _ = is_banned(p_ip) if p_ip else (False, None)
        if not p_banned:
            sanitized_message = html.escape(data['message'])
            message_data = {
                'message': sanitized_message,
                'sender': 'Stranger'
            }
            emit('message', message_data, room=partner_id)
            room_messages[room_id].append({
                'sender': 'Tu' if user_id == request.sid else 'Stranger',
                'message': sanitized_message,
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            })
            logger.info(f'💬 Messaggio da {user_id} a {partner_id}')
        else:
            emit('error', {'message': 'Partner non disponibile'})
    else:
        emit('error', {'message': 'Partner non disponibile'})

@socketio.on('typing')
def handle_typing():
    user_id = request.sid
    user_ip = user_data.get(user_id, {}).get('ip')
    if not user_ip:
        return
    banned, _ = is_banned(user_ip)
    if banned:
        return
    if user_id in user_rooms:
        room_id = user_rooms[user_id]
        if room_id in active_rooms:
            partner_id = next((uid for uid in active_rooms[room_id]['users'] if uid != user_id), None)
            if partner_id:
                emit('typing', room=partner_id)

@socketio.on('stop_typing')
def handle_stop_typing():
    user_id = request.sid
    if user_id in user_rooms:
        room_id = user_rooms[user_id]
        if room_id in active_rooms:
            partner_id = next((uid for uid in active_rooms[room_id]['users'] if uid != user_id), None)
            if partner_id:
                emit('stop_typing', room=partner_id)

@socketio.on('next_partner')
def handle_next_partner():
    user_id = request.sid
    user_ip = user_data.get(user_id, {}).get('ip')
    if not user_ip:
        return
    banned, _ = is_banned(user_ip)
    if banned:
        return
    logger.info(f'⏭️ {user_id} passa al prossimo')
    
    if user_id in user_rooms:
        room_id = user_rooms[user_id]
        if room_id in active_rooms:
            partner_id = next((uid for uid in active_rooms[room_id]['users'] if uid != user_id), None)
            if partner_id:
                leave_room(room_id, sid=partner_id)
                emit('partner_disconnected', room=partner_id)
                if partner_id in user_rooms:
                    del user_rooms[partner_id]
            
            leave_room(room_id, sid=user_id)
            del active_rooms[room_id]
        
        del user_rooms[user_id]
    
    emit_stats()

@socketio.on('report_user')
def handle_report_user(data):
    user_id = request.sid
    user_ip = user_data.get(user_id, {}).get('ip')
    if not user_ip:
        return
    banned, _ = is_banned(user_ip)
    if banned:
        return
    reported_id = data.get('reported_id')
    reason = data.get('reason')
    comment = data.get('comment', '')
    if reported_id and reported_id != user_id:
        report_user(user_id, reported_id, reason, comment)
    else:
        emit('error', {'message': 'ID utente non valido'})

def emit_stats(room=None):
    stats = {
        'online': len(user_data),
        'waiting': len(waiting_users),
        'active_chats': len(active_rooms)
    }
    
    if room:
        socketio.emit('stats_update', stats, room=room)
    else:
        socketio.emit('stats_update', stats)
    
    logger.info(f'📊 Stats: {stats["online"]} online, {stats["waiting"]} in attesa, {stats["active_chats"]} chat attive')

if __name__ == '__main__':
    print("=" * 60)
    print("🎭 ChatRoulette Server (Con Video e Testo)")
    print("=" * 60)
    print("🚀 Server avviato con successo!")
    print("📍 URL: http://localhost:5000")
    print("📹 Supporto WebRTC per videochiamate")
    print("💬 Chat testuale con sistema di segnalazione")
    print("🔒 Sistema ban progressivo e log conversazioni")
    print("=" * 60)
    
    cleanup_expired_bans()
    cleanup_room_messages()
    
    socketio.run(
        app, 
        debug=True, 
        host='0.0.0.0', 
        port=5000,
        allow_unsafe_werkzeug=True
    )