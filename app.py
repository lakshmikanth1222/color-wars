from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room
import os
import logging
from datetime import datetime

# Configure logging for production
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Use environment variable for secret key in production
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'color_wars_render_production_2024')

# Configure Socket.IO for Render
# Configure Socket.IO for Render - Use gevent for better compatibility
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='gevent',  # Changed from eventlet to gevent
    logger=False,
    engineio_logger=False,
    ping_timeout=60,
    ping_interval=25,
    max_http_buffer_size=1e8,
    transports=['websocket', 'polling']
)

# Dictionary to store active game rooms
rooms = {}

class GameRoom:
    def __init__(self, room_id, max_players):
        self.room_id = room_id
        self.max_players = int(max_players)
        # 8x8 Grid initialization
        self.grid = [[{"dots": 0, "owner": None} for _ in range(8)] for _ in range(8)]
        self.players = [] 
        self.turn_index = 0
        self.colors = ["red", "blue", "green", "yellow"]
        self.game_started = False
        self.first_moves_done = {}
        self.created_at = datetime.now()
        self.last_activity = datetime.now()

    def add_player(self, sid, name):
        # Check if player already exists
        for player in self.players:
            if player['id'] == sid:
                return player['color']
                
        if len(self.players) < self.max_players:
            color = self.colors[len(self.players)]
            self.players.append({"id": sid, "name": name, "color": color})
            self.first_moves_done[color] = False
            self.last_activity = datetime.now()
            
            if len(self.players) == self.max_players:
                self.game_started = True
                logger.info(f"Room {self.room_id} is FULL. Starting game.")
            
            return color
        return None

    def remove_player(self, sid):
        for i, player in enumerate(self.players):
            if player['id'] == sid:
                removed_color = player['color']
                removed_name = player['name']
                self.players.pop(i)
                
                if removed_color in self.first_moves_done:
                    del self.first_moves_done[removed_color]
                
                if self.turn_index >= len(self.players):
                    self.turn_index = 0
                
                if len(self.players) < self.max_players:
                    self.game_started = False
                
                self.last_activity = datetime.now()
                logger.info(f"Player {removed_name} removed from room {self.room_id}")
                return removed_name, removed_color
        return None, None

    def handle_click(self, r, c, player_color):
        cell = self.grid[r][c]
        
        is_first_move = not self.first_moves_done[player_color]
        
        if is_first_move:
            if cell["owner"] is None:
                cell["owner"] = player_color
                cell["dots"] = 3
                self.first_moves_done[player_color] = True
                self.last_activity = datetime.now()
                return True
            return False
        else:
            if cell["owner"] == player_color:
                self.add_dot(cell)
                self.last_activity = datetime.now()
                return True
            return False

    def add_dot(self, cell):
        cell["dots"] += 1
        if cell["dots"] >= 4:
            return True
        return False

    def explode(self, r, c, color):
        self.grid[r][c]["dots"] = 0
        self.grid[r][c]["owner"] = None
        
        directions = [(0, 1), (0, -1), (1, 0), (-1, 0)]
        for dr, dc in directions:
            nr, nc = r + dr, c + dc
            if 0 <= nr < 8 and 0 <= nc < 8:
                adjacent_cell = self.grid[nr][nc]
                adjacent_cell["owner"] = color
                adjacent_cell["dots"] += 1
                
                if adjacent_cell["dots"] >= 4:
                    self.explode(nr, nc, color)

    def check_winner(self):
        if not self.game_started:
            return None

        active_owners = set()
        total_dots = 0
        
        for row in self.grid:
            for cell in row:
                if cell["owner"]:
                    active_owners.add(cell["owner"])
                    total_dots += cell["dots"]

        all_first_moves_done = all(self.first_moves_done.values())
        
        if all_first_moves_done and total_dots > 0 and len(active_owners) == 1:
            winner_color = list(active_owners)[0]
            for p in self.players:
                if p['color'] == winner_color:
                    return p['name']
        
        return None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health_check():
    """Health check endpoint for Render"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'active_rooms': len(rooms),
        'total_players': sum(len(room.players) for room in rooms.values())
    })

@app.route('/stats')
def stats():
    """Statistics endpoint"""
    return jsonify({
        'active_rooms': len(rooms),
        'total_players': sum(len(room.players) for room in rooms.values()),
        'rooms': [
            {
                'id': room_id,
                'players': len(room.players),
                'max_players': room.max_players,
                'started': room.game_started,
                'created_at': room.created_at.isoformat(),
                'last_activity': room.last_activity.isoformat()
            }
            for room_id, room in rooms.items()
        ]
    })

@app.route('/cleanup', methods=['POST'])
def cleanup_rooms():
    """Clean up inactive rooms (for maintenance)"""
    cleanup_key = os.environ.get('CLEANUP_KEY')
    if not cleanup_key or request.headers.get('X-Cleanup-Key') != cleanup_key:
        return jsonify({'error': 'Unauthorized'}), 401
    
    rooms_to_delete = []
    for rid, room in rooms.items():
        inactive_time = (datetime.now() - room.last_activity).total_seconds()
        if inactive_time > 3600:  # 1 hour
            rooms_to_delete.append(rid)
    
    for rid in rooms_to_delete:
        del rooms[rid]
    
    return jsonify({
        'cleaned_rooms': rooms_to_delete,
        'remaining_rooms': len(rooms)
    })

@socketio.on('connect')
def handle_connect():
    logger.info(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f"Client disconnected: {request.sid}")

@socketio.on('join_room')
def on_join(data):
    rid = data.get('room', '').strip()
    name = data.get('username', '').strip()
    p_count = data.get('playerCount', 4)
    
    if not rid or not name:
        emit('error', {'msg': 'Room ID and Username are required'}, room=request.sid)
        return
    
    if len(name) > 20:
        emit('error', {'msg': 'Username must be less than 20 characters'}, room=request.sid)
        return
    
    if len(rid) > 50:
        emit('error', {'msg': 'Room ID must be less than 50 characters'}, room=request.sid)
        return
    
    join_room(rid)
    
    if rid in rooms:
        game = rooms[rid]
        existing_names = [p['name'] for p in game.players]
        if name in existing_names:
            emit('error', {'msg': f'Username "{name}" already taken in this room'}, room=request.sid)
            return
        
        if len(game.players) >= game.max_players:
            emit('error', {'msg': 'Room is full!'}, room=request.sid)
            return
    
    if rid not in rooms:
        rooms[rid] = GameRoom(rid, p_count)
    
    game = rooms[rid]
    color = game.add_player(request.sid, name)
    
    if color is not None:
        emit('player_joined', {'username': name, 'color': color}, room=rid, skip_sid=request.sid)
        emit('init_player', {'color': color, 'id': request.sid}, room=request.sid)
        
        state = {
            "grid": game.grid,
            "turn": game.turn_index, 
            "players": game.players, 
            "max": game.max_players, 
            "started": game.game_started,
            "first_moves_done": game.first_moves_done
        }
        emit('update_state', state, room=rid)
        
        logger.info(f"Player {name} joined room {rid} as {color}")
    else:
        emit('error', {'msg': 'Failed to join room'}, room=request.sid)

@socketio.on('make_move')
def on_move(data):
    rid = data.get('room')
    game = rooms.get(rid)
    
    if not game:
        emit('error', {'msg': 'Game room not found'}, room=request.sid)
        return
    
    if not game.game_started:
        emit('error', {'msg': 'Game not started yet'}, room=request.sid)
        return

    row = data.get('r')
    col = data.get('c')
    
    if row is None or col is None or not (0 <= row < 8) or not (0 <= col < 8):
        emit('error', {'msg': 'Invalid coordinates'}, room=request.sid)
        return
    
    if game.turn_index >= len(game.players):
        game.turn_index = 0
    
    curr_p = game.players[game.turn_index]
    
    if request.sid != curr_p['id']:
        emit('error', {'msg': 'Not your turn!'}, room=request.sid)
        return
    
    player_color = curr_p['color']
    
    if game.handle_click(row, col, player_color):
        cell = game.grid[row][col]
        
        if game.first_moves_done[player_color] and cell["dots"] >= 4:
            game.explode(row, col, player_color)
        
        winner = game.check_winner()
        if winner:
            emit('game_over', {'winner': winner}, room=rid)
            logger.info(f"Game over in room {rid}. Winner: {winner}")
        else:
            game.turn_index = (game.turn_index + 1) % len(game.players)
            state = {
                "grid": game.grid, 
                "turn": game.turn_index, 
                "players": game.players, 
                "max": game.max_players,
                "started": True,
                "first_moves_done": game.first_moves_done
            }
            emit('update_state', state, room=rid)
    else:
        is_first_move = not game.first_moves_done[player_color]
        if is_first_move:
            emit('error', {'msg': 'First move must be on an empty cell!'}, room=request.sid)
        else:
            emit('error', {'msg': 'You can only click on your own cells!'}, room=request.sid)

@socketio.on('chat_message')
def handle_chat_message(data):
    rid = data.get('room')
    message = data.get('message', '').strip()
    username = data.get('username')
    color = data.get('color')
    
    if not rid or not message or not username:
        return
    
    if len(message) > 200:
        message = message[:197] + "..."
    
    emit('chat_message', {
        'username': username,
        'message': message,
        'color': color,
        'timestamp': datetime.now().strftime('%H:%M')
    }, room=rid)

@socketio.on('disconnecting')
def handle_disconnecting():
    for rid, game in list(rooms.items()):
        removed_name, removed_color = game.remove_player(request.sid)
        if removed_name:
            emit('player_left', {'username': removed_name, 'color': removed_color}, room=rid)
            
            if not game.players:
                del rooms[rid]
                logger.info(f"Room {rid} deleted (empty)")
            else:
                state = {
                    "grid": game.grid,
                    "turn": game.turn_index,
                    "players": game.players,
                    "max": game.max_players,
                    "started": game.game_started,
                    "first_moves_done": game.first_moves_done
                }
                emit('update_state', state, room=rid)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    # For Render, we need to use the socketio.run() method
    socketio.run(app, 
                 host='0.0.0.0', 
                 port=port, 
                 debug=debug, 
                 allow_unsafe_werkzeug=True,
                 log_output=debug)
