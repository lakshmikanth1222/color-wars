from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
from datetime import datetime
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'color_wars_secret_key'
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="gevent"
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
        self.first_moves_done = {}  # Track which players have made their first move

    def add_player(self, sid, name):
        # Check if player already exists
        for player in self.players:
            if player['id'] == sid:
                return player['color']
                
        if len(self.players) < self.max_players:
            color = self.colors[len(self.players)]
            self.players.append({"id": sid, "name": name, "color": color})
            self.first_moves_done[color] = False
            
            if len(self.players) == self.max_players:
                self.game_started = True
                print(f"Room {self.room_id} is FULL. Starting game.")
            
            return color
        return None

    def remove_player(self, sid):
        for i, player in enumerate(self.players):
            if player['id'] == sid:
                removed_color = player['color']
                removed_name = player['name']
                self.players.pop(i)
                
                # Remove from first moves tracking
                if removed_color in self.first_moves_done:
                    del self.first_moves_done[removed_color]
                
                # Adjust turn index if needed
                if self.turn_index >= len(self.players):
                    self.turn_index = 0
                
                # Reset game if players drop below max
                if len(self.players) < self.max_players:
                    self.game_started = False
                
                print(f"Player {sid} removed from room {self.room_id}")
                return removed_name
        return None

    def handle_click(self, r, c, player_color):
        cell = self.grid[r][c]
        
        # Check if this is player's first move
        is_first_move = not self.first_moves_done[player_color]
        
        if is_first_move:
            # First move: can click anywhere that's empty
            if cell["owner"] is None:
                # Place exactly 3 dots for first move
                cell["owner"] = player_color
                cell["dots"] = 3
                self.first_moves_done[player_color] = True
                return True
            return False
        else:
            # Subsequent moves: can only click on own cells
            if cell["owner"] == player_color:
                self.add_dot(cell)
                return True
            return False

    def add_dot(self, cell):
        cell["dots"] += 1
        if cell["dots"] >= 4:
            return True  # Signal that explosion should happen
        return False

    def explode(self, r, c, color):
        # Reset the exploding cell to neutral
        self.grid[r][c]["dots"] = 0
        self.grid[r][c]["owner"] = None
        
        # Add dots to adjacent cells (up, down, left, right)
        directions = [(0, 1), (0, -1), (1, 0), (-1, 0)]
        for dr, dc in directions:
            nr, nc = r + dr, c + dc
            if 0 <= nr < 8 and 0 <= nc < 8:
                adjacent_cell = self.grid[nr][nc]
                
                # CONVERT adjacent cell to your color
                adjacent_cell["owner"] = color
                adjacent_cell["dots"] += 1
                
                # Check if this causes a chain explosion
                if adjacent_cell["dots"] >= 4:
                    self.explode(nr, nc, color)

    def check_winner(self):
        """Checks if only one player's color remains on the board."""
        if not self.game_started:
            return None

        active_owners = set()
        total_dots = 0
        
        for row in self.grid:
            for cell in row:
                if cell["owner"]:
                    active_owners.add(cell["owner"])
                    total_dots += cell["dots"]

        # Check if all first moves are done
        all_first_moves_done = all(self.first_moves_done.values())
        
        # Only check for winner after first moves are done AND board has dots
        if all_first_moves_done and total_dots > 0 and len(active_owners) == 1:
            winner_color = list(active_owners)[0]
            for p in self.players:
                if p['color'] == winner_color:
                    return p['name']
        
        return None

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_room')
def on_join(data):
    rid = data.get('room')
    name = data.get('username')
    p_count = data.get('playerCount', 4)
    
    if not rid or not name:
        emit('error', {'msg': 'Room ID and Username are required'}, room=request.sid)
        return
    
    join_room(rid)
    
    # Check if username already exists in room
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
        # Notify all players in room about new player
        emit('player_joined', {'username': name}, room=rid, skip_sid=request.sid)
        
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

    # Get coordinates
    row = data.get('r')
    col = data.get('c')
    
    # Validate coordinates
    if row is None or col is None or not (0 <= row < 8) or not (0 <= col < 8):
        emit('error', {'msg': 'Invalid coordinates'}, room=request.sid)
        return
    
    if game.turn_index >= len(game.players):
        game.turn_index = 0
    
    curr_p = game.players[game.turn_index]
    
    # Only process move if it's actually this player's turn
    if request.sid != curr_p['id']:
        emit('error', {'msg': 'Not your turn!'}, room=request.sid)
        return
    
    player_color = curr_p['color']
    
    if game.handle_click(row, col, player_color):
        # Check if we need to handle explosion
        cell = game.grid[row][col]
        
        # If this is not first move and cell reached 4 dots, explode
        if game.first_moves_done[player_color] and cell["dots"] >= 4:
            game.explode(row, col, player_color)
        
        # After processing move, check for win
        winner = game.check_winner()
        if winner:
            emit('game_over', {'winner': winner}, room=rid)
        else:
            # Move to next player's turn
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
        # Get error message based on move type
        is_first_move = not game.first_moves_done[player_color]
        if is_first_move:
            emit('error', {'msg': 'First move must be on an empty cell!'}, room=request.sid)
        else:
            emit('error', {'msg': 'You can only click on your own cells!'}, room=request.sid)

@socketio.on('chat_message')
def handle_chat_message(data):
    rid = data.get('room')
    message = data.get('message')
    username = data.get('username')
    color = data.get('color')
    
    if not rid or not message or not username:
        return
    
    # Broadcast chat message to all players in the room
    emit('chat_message', {
        'username': username,
        'message': message,
        'color': color,
        'timestamp': datetime.now().strftime('%H:%M')
    }, room=rid)

@socketio.on('disconnect')
def on_disconnect():
    print(f"Client disconnected: {request.sid}")
    
    # Clean up disconnected players from all rooms
    rooms_to_delete = []
    for rid, game in rooms.items():
        removed_name = game.remove_player(request.sid)
        if removed_name:
            print(f"Player {request.sid} removed from room {rid}")
            
            # Notify other players
            emit('player_left', {'username': removed_name}, room=rid)
            
            # If room becomes empty, mark for deletion
            if not game.players:
                rooms_to_delete.append(rid)
            else:
                # Update remaining players
                state = {
                    "grid": game.grid,
                    "turn": game.turn_index,
                    "players": game.players,
                    "max": game.max_players,
                    "started": game.game_started,
                    "first_moves_done": game.first_moves_done
                }
                emit('update_state', state, room=rid)
    
    # Delete empty rooms
    for rid in rooms_to_delete:
        del rooms[rid]
        print(f"Room {rid} deleted (empty)")



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    socketio.run(app, host="0.0.0.0", port=port)
