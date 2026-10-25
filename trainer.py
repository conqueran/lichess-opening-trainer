"""Lichess opening trainer.

Builds your opening book (White.bin / Black.bin, Polyglot) from the games you
play. It sends you into a lichess game at the time control you pick, then
walks your game against your book:

- your move is in the book: keep going;
- your move isn't, and it's Stockfish's best move: add it and keep going;
- otherwise: show Stockfish's moves in order down to yours and ask which one
  goes in the book. Choosing your own move adds it and keeps going; choosing
  a different one adds that move, stops, and makes the position a flashcard.

Flashcards use the same spaced repetition as the other trainers, and when
there's nothing left to do it sends you into another game.

Run:  python trainer.py   (requires: pip install chess)
"""

import json
import os
import random
import shutil
import struct
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import chess
import chess.engine
import chess.polyglot

HERE = Path(__file__).resolve().parent
CHESS_DIR = HERE.parent
BOOKS = {chess.WHITE: HERE / "White.bin", chess.BLACK: HERE / "Black.bin"}


def find_stockfish(folders, windows=os.name == "nt"):
    """The first Stockfish program in `folders`, else one on the PATH, else None.

    Any build will do: stockfish-windows-….exe on Windows, or an executable
    stockfish-linux-… (no .exe) elsewhere, which skips the README and license.
    """
    for folder in folders:
        for path in sorted(folder.glob("stockfish*")):
            if not path.is_file():
                continue
            is_exe = path.suffix.lower() == ".exe"
            if is_exe if windows else (not is_exe and os.access(path, os.X_OK)):
                return path
    found = shutil.which("stockfish")
    return Path(found) if found else None


# Stockfish ships inside this folder when the trainer is shared; otherwise it's
# the stockfish folder next to it, or one installed on the system.
STOCKFISH = find_stockfish([HERE / "stockfish", CHESS_DIR / "stockfish"])
STOCKFISH_MISSING = (f"Stockfish wasn't found. Put Stockfish in a folder named 'stockfish' "
                     f"inside {HERE}, or install it so the 'stockfish' command works.")

PROGRESS_FILE = HERE / "progress.json"
CONFIG_FILE = HERE / "config.json"
PORT = 8767

LICHESS = "https://lichess.org"
TIME_CONTROLS = ["1+0", "2+1", "3+0", "3+2", "5+0", "5+3", "10+0", "10+5", "15+10", "30+0", "30+20"]
DEPTHS = [18, 20, 22, 24, 26, 28, 30]
DEFAULTS = {"timeControl": "5+0", "depth": 24, "autoPick": False}
SEEK_POLL = 3      # seconds between checks for the game to start
GAME_POLL = 5      # seconds between checks for the game to finish

MATE_SCORE = 10000
ENGINE_THREADS = max(1, (os.cpu_count() or 2) - 1)
ENGINE_HASH_MB = 256

REPEAT_GAP = 5  # a missed card comes back after this many other cards
# Days until the next review after a correct answer, indexed by the new box
# (the number of correct answers in a row); the last entry (100 years) repeats.
INTERVAL_DAYS = [0, 1, 3, 7, 14, 30, 60, 120, 240, 480, 960, 1920, 3840, 7680,
                 15360, 30720, 36500]
DAY = 86400

ACTIVE_PHASES = ("seeking", "playing", "analyzing")

lock = threading.Lock()
session = {}
# fen/side/move: the position the analysis is checking right now, for the board.
game = {"phase": "idle", "message": "", "detail": "", "gameUrl": None,
        "fen": None, "side": None, "move": None, "token": 0}


# ---------------------------------------------------------------- storage

def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_progress():
    """Load {"deck", "cards", "sessions", "games", "pending"}.

    deck maps FEN to a card made where you chose a different move than you
    played. cards maps FEN to a review record {"box", "due", "right",
    "wrong"}: box is the number of correct answers in a row and due is the
    Unix time of the next review. games logs every game that was walked.
    pending is the decision waiting for you, or None.
    """
    progress = load_json(PROGRESS_FILE)
    for key in ("deck", "cards"):
        progress.setdefault(key, {})
    for key in ("sessions", "games"):
        progress.setdefault(key, [])
    progress.setdefault("pending", None)
    return progress


def update_progress(change):
    """Apply change(progress) to the live progress (the session's, if running) and save."""
    with lock:
        progress = session["progress"] if session else load_progress()
        result = change(progress)
        save_json(PROGRESS_FILE, progress)
        return result


def config():
    return {**DEFAULTS, **load_json(CONFIG_FILE)}


def username():
    return config().get("username")


# ---------------------------------------------------------------- book

def encode_move(board, move):
    """Polyglot move encoding; castling is written as the king taking its rook."""
    to = move.to_square
    if board.is_castling(move):
        rank = chess.square_rank(move.from_square)
        to = chess.square(7 if board.is_kingside_castling(move) else 0, rank)
    promo = move.promotion - 1 if move.promotion else 0
    return to | move.from_square << 6 | promo << 12


class Book:
    """A Polyglot book held in memory and rewritten whole when a move is added."""

    ENTRY = struct.Struct(">QHHI")

    def __init__(self, path):
        self.path = path
        self.entries = {}  # zobrist key -> {raw move: (weight, learn)}
        data = path.read_bytes() if path.exists() else b""
        for i in range(0, len(data) - len(data) % 16, 16):
            key, raw, weight, learn = self.ENTRY.unpack_from(data, i)
            self.entries.setdefault(key, {})[raw] = (weight, learn)

    def moves(self, board):
        """Book moves here; weight-0 moves count only if nothing else does."""
        here = self.entries.get(chess.polyglot.zobrist_hash(board), {})
        raws = {raw for raw, (w, _) in here.items() if w > 0} or set(here)
        return [m for m in board.legal_moves if encode_move(board, m) in raws]

    def add(self, board, move):
        """Add a move with weight 1; returns False if it was already there."""
        key, raw = chess.polyglot.zobrist_hash(board), encode_move(board, move)
        here = self.entries.setdefault(key, {})
        if raw in here:
            return False
        here[raw] = (1, 0)
        self.save()
        return True

    def save(self):
        data = b"".join(self.ENTRY.pack(key, raw, weight, learn)
                        for key in sorted(self.entries)
                        for raw, (weight, learn) in sorted(self.entries[key].items(),
                                                           key=lambda e: -e[1][0]))
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self.path)

    def size(self):
        return sum(len(v) for v in self.entries.values())


def ensure_books():
    for path in BOOKS.values():
        if not path.exists():
            path.write_bytes(b"")


# ---------------------------------------------------------------- lichess

def lichess_get(path, accept="application/json"):
    """GET a lichess API path; returns parsed JSON, or None on 404."""
    req = urllib.request.Request(LICHESS + path, headers={
        "Accept": accept, "User-Agent": "LichessOpeningTrainer (personal study tool)"})
    while True:
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                text = res.read().decode("utf-8").strip()
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None
            if err.code == 429:  # lichess asks clients to wait a full minute
                time.sleep(60)
                continue
            raise
        if not text:
            return None
        return json.loads(text)


def current_game(user):
    return lichess_get(f"/api/user/{user}/current-game?moves=false")


def export_game(game_id):
    return lichess_get(f"/game/export/{game_id}?clocks=false&evals=false")


def player_name(g, color):
    p = g.get("players", {}).get(color, {})
    return p.get("user", {}).get("name") or ("AI level %s" % p["aiLevel"] if "aiLevel" in p else "Anonymous")


def game_info(g, side):
    color = "white" if side == chess.WHITE else "black"
    opp = "black" if color == "white" else "white"
    return {
        "id": g["id"],
        "url": f"{LICHESS}/{g['id']}/{color}",
        "date": time.strftime("%Y-%m-%d", time.localtime(g.get("createdAt", 0) / 1000)),
        "opponent": player_name(g, opp),
        "rating": g.get("players", {}).get(opp, {}).get("rating"),
        "speed": g.get("clock") and f"{g['clock']['initial'] // 60}+{g['clock']['increment']}",
    }


# ---------------------------------------------------------------- analysis

class Cancelled(Exception):
    pass


def eval_text(info):
    """Engine score from White's point of view, e.g. +0.31 or #-4."""
    white = info["score"].white()
    if white.is_mate():
        return f"#{white.mate()}"
    return f"{white.score() / 100:+.2f}"


def search(engine, board, depth, cancelled, **options):
    """Run one depth-limited search, raising Cancelled once cancelled() is true."""
    with engine.analysis(board, chess.engine.Limit(depth=depth), **options) as analysis:
        for _ in analysis:
            if cancelled():
                analysis.stop()
                raise Cancelled
        return analysis.multipv


def rank(engine, board, move, depth, cancelled, report):
    """Stockfish's moves best-first, down to and including `move`, all at `depth`.

    Asks for the best move only, then the best 2, 3, … until `move` appears.
    """
    legal = board.legal_moves.count()
    k = 1
    while True:
        infos = search(engine, board, depth, cancelled, multipv=k)
        firsts = [i["pv"][0] for i in infos if i.get("pv")]
        if move in firsts:
            return infos[:firsts.index(move) + 1]
        if k >= legal:  # can't happen unless the engine drops lines; fall back to a direct search
            return infos + search(engine, board, depth, cancelled, root_moves=[move])
        k += 1
        report(f"top {k} so far")


def option(board, info, rank_no, played, book):
    m = info["pv"][0]
    return {"uci": m.uci(), "san": board.san(m), "eval": eval_text(info), "rank": rank_no,
            "played": m == played, "book": m in book}


def label(board, san):
    return f"{board.fullmove_number}{'.' if board.turn == chess.WHITE else '...'}{san}"


def walk(info, moves, side, start, depth, added, report, cancelled, show=lambda board, move: None,
         auto=False):
    """Walk your moves from ply `start`, adding accepted moves to your book.

    show(board, move) is called with each position Stockfish is about to check.
    With `auto`, a deviation isn't ranked: your book move (or, out of book,
    Stockfish's top move) is picked for you.

    Returns {"pending": decision} when a choice is needed, the same plus
    "auto": uci when `auto` picked the move, or {"added": n} when the game
    ends. Raises Cancelled.
    """
    book = Book(BOOKS[side])
    board = chess.Board()
    for san in moves[:start]:
        board.push_san(san)
    if STOCKFISH is None:
        raise RuntimeError(STOCKFISH_MISSING)
    engine = chess.engine.SimpleEngine.popen_uci(str(STOCKFISH))
    try:
        engine.configure({"Threads": ENGINE_THREADS, "Hash": ENGINE_HASH_MB})
        for ply in range(start, len(moves)):
            if cancelled():
                raise Cancelled
            san = moves[ply]
            move = board.parse_san(san)
            if board.turn == side:
                in_book = book.moves(board)

                def decision(options):
                    played = next(o for o in options if o["played"])
                    return {"game": info, "moves": moves, "side": "white" if side == chess.WHITE else "black",
                            "ply": ply, "fen": board.fen(), "path": moves[:ply], "depth": depth,
                            "played": played, "options": options, "added": added,
                            "inBook": bool(in_book)}

                if move in in_book:
                    added += add_previous(book, board, moves, ply)
                elif auto and in_book:
                    # You left your own book: your book move is the answer, no engine needed.
                    options = [{"uci": m.uci(), "san": board.san(m), "eval": None, "rank": None,
                                "played": False, "book": True} for m in in_book]
                    options.append({"uci": move.uci(), "san": san, "eval": None, "rank": None,
                                    "played": True, "book": False})
                    return {"pending": decision(options), "auto": in_book[0].uci()}
                else:
                    name = label(board, san)
                    show(board, move)
                    report(f"Checking {name} (depth {depth})…")
                    if auto:
                        ranked = search(engine, board, depth, cancelled, multipv=1)
                        if ranked[0]["pv"][0] != move:  # your move's eval, to show the gap on the card
                            ranked = ranked + search(engine, board, depth, cancelled, root_moves=[move])
                    else:
                        ranked = rank(engine, board, move, depth, cancelled,
                                      lambda more: report(f"Finding where {name} ranks (depth {depth}, {more})…"))
                    if len(ranked) == 1 and not in_book:
                        added += add_previous(book, board, moves, ply)
                        added += book.add(board, move)
                    elif auto:
                        options = [option(board, ranked[0], 1, move, in_book),
                                   {**option(board, ranked[1], None, move, in_book), "rank": None}]
                        return {"pending": decision(options), "auto": options[0]["uci"]}
                    else:
                        options = [option(board, i, n + 1, move, in_book) for n, i in enumerate(ranked)]
                        listed = {o["uci"] for o in options}
                        for m in in_book:  # book moves ranked below yours, with their own evals
                            if m.uci() not in listed:
                                extra = search(engine, board, depth, cancelled, root_moves=[m])[0]
                                options.append({**option(board, extra, None, move, in_book), "rank": None})
                        return {"pending": decision(options)}
            board.push(move)
        return {"added": added}
    finally:
        engine.quit()


def add_previous(book, board, moves, ply):
    """Add the opponent's move that led here (if any) to the book."""
    if ply == 0:
        return 0
    before = board.copy()
    last = before.pop()
    return int(book.add(before, last))


# ---------------------------------------------------------------- game loop

def set_game(token, **fields):
    """Update the game status, unless a newer /play, /cancel or /decide replaced this run."""
    with lock:
        if game["token"] != token:
            return False
        game.update(fields)
        return True


def shower(token):
    """A walk `show` callback that puts the checked position in the game status."""
    return lambda board, move: set_game(token, fen=board.fen(), move=move.uci(),
                                        side="white" if board.turn == chess.WHITE else "black")


def new_token(**fields):
    with lock:
        game["token"] += 1
        game.update(fields)
        return game["token"]


def side_word(side):
    return "White" if side in (chess.WHITE, "white") else "Black"


def moves_word(n):
    return "1 move" if n == 1 else f"{n} moves"


def finish(token, result, info, side):
    """Store a walk's outcome: a decision to make, or the end of the game."""
    if "pending" in result:
        p = result["pending"]
        if result.get("auto"):
            apply_choice(p, result["auto"], picked=True)
            return
        update_progress(lambda progress: progress.update(pending=p))
        set_game(token, phase="decide", message="Choose a move for your book.", detail="")
        return
    added = result["added"]
    log_game(info["id"], f"{added} added", None)
    set_game(token, phase="done",
             message=f"Added {moves_word(added)} to your {side_word(side)} book. No card this game.",
             detail="You played Stockfish's best move every time you were out of book.")


def log_game(game_id, result, fen):
    def change(progress):
        progress["games"].append({"id": game_id, "date": time.strftime("%Y-%m-%d %H:%M"),
                                  "result": result, "fen": fen})
    update_progress(change)


def game_worker(token, user, tc, depth, auto):
    def alive():
        with lock:
            return game["token"] == token

    try:
        before = current_game(user)
        before_id = before["id"] if before else None
        webbrowser.open(f"{LICHESS}/#pool/{tc}")
        set_game(token, phase="seeking", message=f"Waiting for your {tc} game…",
                 detail="Lichess is open in your browser and joining the pool.", gameUrl=None)

        while alive():
            g = current_game(user)
            if g and g["id"] != before_id:
                break
            time.sleep(SEEK_POLL)
        else:
            return
        game_id = g["id"]
        opponent = player_name(g, "black" if player_name(g, "white").lower() == user.lower() else "white")
        set_game(token, phase="playing", message=f"Game in progress vs {opponent}",
                 detail="Good luck! Come back here when it's over.", gameUrl=f"{LICHESS}/{game_id}")

        while alive():
            g = export_game(game_id)
            if g and g.get("status") not in ("created", "started"):
                break
            time.sleep(GAME_POLL)
        else:
            return

        moves = g.get("moves", "").split()
        if g["status"] in ("aborted", "noStart") or len(moves) < 2:
            set_game(token, phase="idle", message="The game was aborted.", detail="")
            return
        if g.get("variant", "standard") != "standard" or g.get("initialFen"):
            set_game(token, phase="idle", message="That wasn't a standard game, so it was skipped.", detail="")
            return
        side = chess.WHITE if player_name(g, "white").lower() == user.lower() else chess.BLACK

        set_game(token, phase="analyzing", message=f"Walking your game vs {opponent} through your book…",
                 detail="")
        info = game_info(g, side)
        result = walk(info, moves, side, 0, depth, 0,
                      report=lambda text: set_game(token, detail=text), cancelled=lambda: not alive(),
                      show=shower(token), auto=auto)
        finish(token, result, info, side)
    except Cancelled:
        pass
    except Exception as err:  # network trouble, engine crash: report and stop
        set_game(token, phase="idle", message="Something went wrong.", detail=str(err))


def start_game():
    cfg = config()
    token = new_token(phase="seeking", message="Opening lichess…", detail="", gameUrl=None, fen=None, move=None)
    threading.Thread(target=game_worker,
                     args=(token, cfg["username"], cfg["timeControl"], cfg["depth"], cfg["autoPick"]),
                     daemon=True).start()


def cancel_game():
    new_token(phase="idle", message="Cancelled.", detail="", gameUrl=None, fen=None, move=None)


def decide(uci):
    """Apply your choice for the pending decision. Returns an error string or None."""
    with lock:
        progress = session["progress"] if session else load_progress()
        p = progress["pending"]
    if not p:
        return "Nothing to decide."
    return apply_choice(p, uci)


def apply_choice(p, uci, picked=False):
    """Put the chosen move for decision `p` in the book, then continue or make the card.

    picked is true when "Pick for me" chose the move rather than you.
    Returns an error string or None.
    """
    choice = next((o for o in p["options"] if o["uci"] == uci), None)
    if choice is None:
        return "That move isn't one of the options."
    side = chess.WHITE if p["side"] == "white" else chess.BLACK
    board = chess.Board()
    for san in p["moves"][:p["ply"]]:
        board.push_san(san)
    book = Book(BOOKS[side])
    added = p["added"] + add_previous(book, board, p["moves"], p["ply"])
    added += book.add(board, chess.Move.from_uci(uci))

    if choice["played"]:
        # Your own move: keep walking the game from the next move.
        update_progress(lambda progress: progress.update(pending=None))
        after = board.copy()
        after.push(chess.Move.from_uci(uci))
        token = new_token(phase="analyzing", message=f"Added {choice['san']}. Continuing through your game…",
                          fen=after.fen(), side=p["side"], move=None,
                          detail="", gameUrl=None)

        def resume():
            try:
                result = walk(p["game"], p["moves"], side, p["ply"] + 1, p["depth"], added,
                              report=lambda text: set_game(token, detail=text),
                              cancelled=lambda: game["token"] != token, show=shower(token))
                finish(token, result, p["game"], side)
            except Cancelled:
                pass
            except Exception as err:
                set_game(token, phase="idle", message="Something went wrong.", detail=str(err))

        threading.Thread(target=resume, daemon=True).start()
        return None

    # A different move: it goes in the book, the walk stops, and this is the card.
    card = {"fen": p["fen"], "side": p["side"], "path": p["path"], "ply": p["ply"],
            "game": {**p["game"], "url": f"{p['game']['url']}#{p['ply']}"},
            "legal": [m.uci() for m in board.legal_moves],
            "chosen": choice, "played": p["played"], "options": p["options"], "depth": p["depth"]}

    def change(progress):
        progress["pending"] = None
        if card["fen"] not in progress["deck"]:
            progress["deck"][card["fen"]] = card
            if session:
                session["new"].append(card["fen"])
        progress["games"].append({"id": p["game"]["id"], "date": time.strftime("%Y-%m-%d %H:%M"),
                                  "result": f"{added} added, card", "fen": card["fen"]})
    update_progress(change)
    if picked:
        source = "your book move" if choice["book"] else "Stockfish's best"
        detail = (f"Move {board.fullmove_number}: you played {p['played']['san']}; "
                  f"{source} is {choice['san']}.")
    else:
        detail = f"Move {board.fullmove_number}: you chose {choice['san']} over your {p['played']['san']}."
    new_token(phase="done", fen=None, move=None,
              message=f"Added {moves_word(added)} to your {side_word(side)} book, and a new card.",
              detail=detail, gameUrl=None)
    return None


def game_state():
    with lock:
        state = {k: v for k, v in game.items() if k != "token"}
        progress = session["progress"] if session else load_progress()
        pending = progress["pending"]
    if pending and state["phase"] not in ACTIVE_PHASES:
        state["phase"] = "decide"
        state["pending"] = {k: v for k, v in pending.items() if k != "moves"}
    cfg = config()
    state.update(username=cfg.get("username"), timeControl=cfg["timeControl"], depth=cfg["depth"],
                 autoPick=cfg["autoPick"],
                 timeControls=TIME_CONTROLS, depths=DEPTHS)
    return state


# ---------------------------------------------------------------- flashcards

def start_session():
    progress = load_progress()
    save_json(PROGRESS_FILE, progress)
    new = [fen for fen in progress["deck"] if fen not in progress["cards"]]
    random.shuffle(new)
    session.clear()
    session.update(
        progress=progress, new=new,
        relearn=[],      # [fen, served count at which it may come back]
        served=0, current=None,
        answered=0, correct=0, unique=set(),
        history=None,    # this session's entry in progress["sessions"]
    )


def deck():
    return session["progress"]["deck"]


def records():
    return session["progress"]["cards"]


def pick_fen():
    """Choose the next card: relearn misses, then due reviews, then new cards.

    Returns None when nothing is left to do, which means it's time to play.
    """
    cards, recs, current = deck(), records(), session["current"]
    relearn = session["relearn"]
    waiting = {fen for fen, _ in relearn}

    for i, (fen, ready_at) in enumerate(relearn):
        if session["served"] >= ready_at and fen != current:
            return relearn.pop(i)[0]

    now = time.time()
    due = [(recs[fen]["box"], recs[fen]["due"], fen) for fen in cards
           if fen in recs and recs[fen]["due"] <= now
           and fen not in waiting and fen != current]
    if due:
        return min(due)[2]

    while session["new"]:
        fen = session["new"].pop()
        if fen not in recs:
            return fen

    # Misses must be answered right before the deck counts as clear.
    if relearn:
        return relearn.pop(0)[0]
    return None


def book_sizes():
    return {"white": Book(BOOKS[chess.WHITE]).size(), "black": Book(BOOKS[chess.BLACK]).size()}


def stats():
    cards, recs = deck(), records()
    now = time.time()
    seen = sum(1 for fen in cards if fen in recs)
    return {
        "total": len(cards),
        "games": len(session["progress"]["games"]),
        "book": book_sizes(),
        "answered": session["answered"],
        "correct": session["correct"],
        "sessionUnique": len(session["unique"]),
        "new": len(cards) - seen,
        "learned": sum(1 for fen in cards if fen in recs and recs[fen]["box"] >= 1),
        "due": sum(1 for fen in cards if fen in recs and recs[fen]["due"] <= now),
    }


def next_card():
    fen = pick_fen()
    session["current"] = fen
    if fen is None:
        return {"card": None, "done": True, "stats": stats()}
    session["served"] += 1
    card = deck()[fen]
    rec = records().get(fen)
    if rec is None:
        status = "new"
    elif rec["box"] == 0:
        status = "missed"
    else:
        status = "review"
    return {
        "card": {"fen": fen, "side": card["side"], "path": card["path"],
                 "legal": card["legal"], "game": card["game"],
                 "status": status, "streak": rec["box"] if rec else 0},
        "done": False,
        "stats": stats(),
    }


def card_answers(card):
    """The book moves at the card's position now, or the move you chose if the book lost it."""
    board = chess.Board(card["fen"])
    side = chess.WHITE if card["side"] == "white" else chess.BLACK
    moves = Book(BOOKS[side]).moves(board)
    if not moves:
        return [{"uci": card["chosen"]["uci"], "san": card["chosen"]["san"]}]
    return [{"uci": m.uci(), "san": board.san(m)} for m in moves]


def answer(fen, uci):
    card = deck()[fen]
    answers = card_answers(card)
    correct = any(a["uci"] == uci for a in answers)
    rec = records().setdefault(fen, {"box": 0, "due": 0, "right": 0, "wrong": 0})
    now = time.time()
    if correct:
        rec["right"] += 1
        rec["box"] += 1
        days = INTERVAL_DAYS[min(rec["box"], len(INTERVAL_DAYS) - 1)]
        rec["due"] = now + days * DAY
    else:
        rec["wrong"] += 1
        rec["box"] = 0
        rec["due"] = now
        days = 0
        session["relearn"].append([fen, session["served"] + REPEAT_GAP])

    session["answered"] += 1
    session["correct"] += correct
    session["unique"].add(fen)
    if session["history"] is None:
        session["history"] = {"date": time.strftime("%Y-%m-%d %H:%M")}
        session["progress"]["sessions"].append(session["history"])
    session["history"].update(unique=len(session["unique"]),
                              answered=session["answered"],
                              correct=session["correct"])
    save_json(PROGRESS_FILE, session["progress"])
    return {"correct": correct, "answers": answers, "chosen": card["chosen"],
            "played": card["played"], "options": card["options"], "depth": card["depth"],
            "nextReviewDays": days, "stats": stats()}


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/pieces/") and self.path.endswith(".svg"):
            piece = HERE / "pieces" / Path(self.path).name
            if not piece.is_file():
                return self.send_error(404)
            body = piece.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/state":
            self.send_json(game_state())
        elif self.path == "/next":
            with lock:
                if not session:
                    return self.send_json({"error": "session not started"}, 400)
                self.send_json(next_card())
        else:
            self.send_error(404)

    def do_POST(self):
        data = self.read_json()
        if self.path == "/config":
            name = str(data.get("username", "")).strip()
            try:
                user = lichess_get(f"/api/user/{name}") if name else None
            except (urllib.error.URLError, OSError) as err:
                return self.send_json({"error": f"Couldn't reach lichess: {err}"}, 502)
            if not user:
                return self.send_json({"error": f"No lichess user named “{name}”."}, 400)
            save_json(CONFIG_FILE, {**load_json(CONFIG_FILE), "username": user["username"]})
            return self.send_json(game_state())
        if self.path == "/settings":
            cfg = load_json(CONFIG_FILE)
            if data.get("timeControl") in TIME_CONTROLS:
                cfg["timeControl"] = data["timeControl"]
            if data.get("depth") in DEPTHS:
                cfg["depth"] = data["depth"]
            if isinstance(data.get("autoPick"), bool):
                cfg["autoPick"] = data["autoPick"]
            save_json(CONFIG_FILE, cfg)
            return self.send_json(game_state())
        if self.path == "/play":
            if not username():
                return self.send_json({"error": "no username"}, 400)
            if game_state()["phase"] == "decide":
                return self.send_json({"error": "Choose a move for your book first."}, 400)
            start_game()
            return self.send_json(game_state())
        if self.path == "/cancel":
            cancel_game()
            return self.send_json(game_state())
        if self.path == "/decide":
            error = decide(str(data.get("uci", "")))
            if error:
                return self.send_json({"error": error}, 400)
            return self.send_json(game_state())
        with lock:
            if self.path == "/start":
                start_session()
                self.send_json(stats())
            elif self.path == "/answer":
                if not session or data.get("fen") not in deck():
                    return self.send_json({"error": "unknown position"}, 400)
                self.send_json(answer(data["fen"], data.get("uci", "")))
            else:
                self.send_error(404)


class Server(ThreadingHTTPServer):
    # On Windows, address reuse lets a second copy bind the same port while
    # the first keeps answering requests.
    allow_reuse_address = False


def main():
    url = f"http://localhost:{PORT}/"
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        # Already running in another window: just open the page.
        webbrowser.open(url)
        return
    ensure_books()
    if STOCKFISH is None:
        print("WARNING: " + STOCKFISH_MISSING)
    print(f"Lichess opening trainer running at {url}  (close this window to stop)")
    threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
