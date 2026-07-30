from __future__ import annotations
from typing import Dict, List, Optional, Set, Tuple, Callable
import sys


class Item:
    """Represents a collectible or interactive item in the game world."""
    def __init__(self, name: str, description: str, on_use: Optional[Callable[[GameEngine, GameState], str]] = None):
        self.name = name
        self.description = description
        self.on_use = on_use


class Room:
    """Represents a location within the game world."""
    def __init__(self, name: str, description: str, exits: Dict[str, str],
                 items: Optional[List[str]] = None,
                 locked_exits: Optional[Dict[str, Dict[str, object]]] = None):
        self.name = name
        self.description = description
        self.exits = exits
        self.items: List[str] = items or []
        self.locked_exits: Dict[str, Dict[str, object]] = locked_exits or {}


class GameState:
    """Tracks the current state of the game including player progress and world state."""
    def __init__(self):
        self.current_room: str = "entrance_hall"
        self.inventory: List[str] = []
        self.visited_rooms: Set[str] = set()
        self.flags: Dict[str, bool] = {}
        self.history: List[str] = []
        self.rooms: Dict[str, Room] = {}
        self.items: Dict[str, Item] = {}
        self.won: bool = False


class GameEngine:
    """Core engine handling input parsing, command routing, and state management."""
    def __init__(self, state: GameState):
        self.state = state

    def run(self) -> None:
        """Starts the main game loop."""
        print_banner()
        print(self.describe_current_room())
        while not self.state.won:
            try:
                raw = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                print("\n\nGoodbye! Thanks for playing.")
                return

            cmd, args = self.parse_input(raw)
            if cmd == "noop":
                continue

            output = self.dispatch(cmd, args)
            self.state.history.append(output)

            print("\n" + "=" * 40)
            print(output)
            print("=" * 40)

            if cmd == "quit":
                print("\nGoodbye! Thanks for playing.")
                break

            if self.state.won:
                print("\n🏆 VICTORY! 🏆")
                print("You have uncovered the ancient scroll's secret. The dungeon yields its treasure.")
                break

    def parse_input(self, raw: str) -> Tuple[str, List[str]]:
        """Parses raw user input into a normalized command and arguments."""
        text = raw.strip().lower().replace("-", " ")
        if not text:
            return ("noop", [])

        # Synonym replacements
        synonyms = {
            "examine": "look at", "inspect": "look at", "look at": "look at",
            "grab": "take", "pick up": "take", "take": "take",
            "inv": "inventory", "bag": "inventory", "inventory": "inventory",
            "go": "go", "move": "go", "walk": "go"
        }
        for old, new in synonyms.items():
            if text.startswith(old + " ") or text == old:
                text = new + text[len(old):]
                break

        tokens = text.split()
        cmd = tokens[0]
        args = tokens[1:]

        # Handle direction-only commands
        dir_match = self._match_direction(cmd)
        if dir_match:
            return ("go", [dir_match])

        return (cmd, args)

    def _match_direction(self, text: str) -> Optional[str]:
        """Matches a direction string, supporting abbreviations and partial matches."""
        dirs = {"n": "north", "s": "south", "e": "east", "w": "west",
                "north": "north", "south": "south", "east": "east", "west": "west"}
        if text in dirs:
            return dirs[text]
        for k, v in dirs.items():
            if v.startswith(text) or text.startswith(v):
                return v
        return None

    def _find_item(self, name: str, in_room: bool = True) -> Optional[str]:
        """Finds an item ID by partial case-insensitive match."""
        target = name.lower()
        sources = []
        if in_room:
            sources.extend(self.state.rooms[self.state.current_room].items)
        sources.extend(self.state.inventory)
        for item_id in sources:
            if target in item_id or item_id in target:
                return item_id
        return None

    def dispatch(self, cmd: str, args: List[str]) -> str:
        """Routes commands to appropriate handler methods."""
        handlers = {
            "look": self.cmd_look,
            "look_at": self.cmd_look_at,
            "go": self.cmd_go,
            "take": self.cmd_take,
            "drop": self.cmd_drop,
            "inventory": self.cmd_inventory,
            "use": self.cmd_use,
            "help": self.cmd_help,
            "history": self.cmd_history,
            "quit": self.cmd_quit
        }
        handler = handlers.get(cmd)
        return handler(args) if handler else f"Unknown command: '{cmd}'. Type 'help' for available commands."

    def describe_current_room(self) -> str:
        """Returns a formatted description of the current room."""
        room = self.state.rooms[self.state.current_room]
        out = f"--- {room.name} ---\n{room.description}"
        if room.items:
            names = [self.state.items[i].name for i in room.items]
            out += f"\nYou see: {', '.join(names)}"
        return out

    def cmd_look(self, args: List[str]) -> str:
        """Prints the current room description and visible items."""
        return self.describe_current_room()

    def cmd_look_at(self, args: List[str]) -> str:
        """Prints the description of a specified item."""
        if not args:
            return "Look at what? Specify an item."
        item_id = self._find_item(args[0])
        return self.state.items[item_id].description if item_id else f"There is no '{args[0]}' in sight."

    def cmd_go(self, args: List[str]) -> str:
        """Moves the player to an adjacent room."""
        if not args:
            return "Go where? Specify a direction (north, south, east, west)."
        direction = args[0]
        room = self.state.rooms[self.state.current_room]

        if direction not in room.exits:
            return f"There is no exit to the {direction}."

        target_id = room.exits[direction]

        # Check locked exits
        if direction in room.locked_exits:
            lock = room.locked_exits[direction]
            if lock["locked"]:
                if lock["key_needed"]:
                    if lock["key_needed"] in self.state.inventory:
                        lock["locked"] = False
                        self.state.flags["door_unlocked"] = True
                        return f"You use the {lock['key_needed']} and the lock clicks open. {lock['unlock_msg']}"
                    return f"{lock['unlock_msg']} You need a {lock['key_needed']} to proceed."
                return lock["unlock_msg"]

        self.state.current_room = target_id
        self.state.visited_rooms.add(target_id)
        self._update_dynamic_descriptions()
        return self.describe_current_room()

    def cmd_take(self, args: List[str]) -> str:
        """Adds an item from the current room to the player's inventory."""
        if not args:
            return "Take what? Specify an item."
        item_id = self._find_item(args[0], in_room=True)
        if not item_id:
            return f"There is no '{args[0]}' here."
        item = self.state.items[item_id]

        self.state.rooms[self.state.current_room].items.remove(item_id)
        self.state.inventory.append(item_id)

        # Special behavior: torch changes Armory description
        if item_id == "torch":
            self.state.rooms["armory"].description = self.state.rooms["armory"].description.replace(
                "dark", "lit by a flickering light"
            ).replace("darkness", "flickering light")
            return f"You pick up the {item.name}. The nearby Armory is now lit by its flickering light."
        return f"You pick up the {item.name}."

    def cmd_drop(self, args: List[str]) -> str:
        """Removes an item from inventory and places it in the current room."""
        if not args:
            return "Drop what? Specify an item."
        item_id = self._find_item(args[0], in_room=False)
        if not item_id:
            return f"You don't have a '{args[0]}'."
        item = self.state.items[item_id]

        self.state.inventory.remove(item_id)
        self.state.rooms[self.state.current_room].items.append(item_id)
        return f"You drop the {item.name}."

    def cmd_inventory(self, args: List[str]) -> str:
        """Lists all items currently carried by the player."""
        if not self.state.inventory:
            return "Your inventory is empty."
        names = [self.state.items[i].name for i in self.state.inventory]
        return f"Inventory: {', '.join(names)}"

    def cmd_use(self, args: List[str]) -> str:
        """Activates an item's on_use callback."""
        if not args:
            return "Use what? Specify an item."
        item_id = self._find_item(args[0], in_room=False)
        if not item_id:
            return f"You don't have a '{args[0]}'."
        item = self.state.items[item_id]
        if item.on_use:
            return item.on_use(self, self.state)
        return f"There's nothing to use about the {item.name}."

    def cmd_help(self, args: List[str]) -> str:
        """Prints a list of available commands."""
        return (
            "Available commands:\n"
            "  look / examine [item] - Describe room or item\n"
            "  go/move <dir> / <dir> - Move north, south, east, west\n"
            "  take/grab <item> - Pick up an item\n"
            "  drop <item> - Drop an item\n"
            "  inventory / i - Check inventory\n"
            "  use <item> - Use an item\n"
            "  help - Show this message\n"
            "  history - Show recent messages\n"
            "  quit - Exit game"
        )

    def cmd_history(self, args: List[str]) -> str:
        """Prints the last 10 game events/messages."""
        if not self.state.history:
            return "No history yet."
        recent = self.state.history[-10:]
        return "Recent messages:\n" + "\n".join(f"  {msg}" for msg in recent)

    def cmd_quit(self, args: List[str]) -> str:
        """Signals the game loop to terminate."""
        return "quit"

    def _update_dynamic_descriptions(self) -> None:
        """Updates room descriptions based on game state flags."""
        # Library lever effect
        if self.state.flags.get("library_west_unlocked"):
            lib = self.state.rooms["library"]
            if "bookshelf blocks" in lib.description:
                lib.description = lib.description.replace(
                    "A dusty bookshelf blocks the way south.", "The dusty bookshelf has been pushed aside."
                )


def print_banner() -> None:
    """Prints a styled ASCII banner to start the game."""
    print("""
   ╔══════════════════════════════════════════╗
   ║          DUNGEON OF SHADOWS              ║
   ║      A Text-Based Adventure Engine       ║
   ╚══════════════════════════════════════════╝
   """)


def create_world() -> GameState:
    """Constructs the dungeon world and returns the initial game state."""
    rusted_key = Item("rusted key", "A heavy, rusted key. It looks old and sturdy.")
    torch = Item("torch", "A wooden torch wrapped in oil-soaked cloth.")
    golden_coin = Item("golden coin", "A heavy gold coin stamped with the face of a grumpy king. 'Worthless to a beggar, priceless to a king.'")
    scroll = Item("ancient scroll", "A fragile parchment with faded ink.")

    def lever_use(engine: GameEngine, state: GameState) -> str:
        state.flags["library_west_unlocked"] = True
        state.rooms["library"].locked_exits["west"]["locked"] = False
        state.rooms["library"].description = state.rooms["library"].description.replace(
            "A dusty bookshelf blocks the way south.", "The dusty bookshelf has been pushed aside."
        )
        return "You pull the lever. A grinding sound echoes as a hidden passage opens to the west."

    lever = Item("lever", "A metal lever protruding from the wall. It seems loose.", on_use=lever_use)

    def scroll_use(engine: GameEngine, state: GameState) -> str:
        state.won = True
        return (
            "You carefully unroll the ancient scroll.\n"
            "It reads: 'The true treasure was the journey itself, but you found the golden coin anyway. Congratulations!'\n"
            "🏆 SECRET ENDING UNLOCKED 🏆"
        )

    scroll.on_use = scroll_use

    entrance = Room("Entrance Hall", "A dimly lit stone hall with rough-hewn walls. The air smells of damp earth.",
                    {"north": "library", "east": "armory", "south": "garden"}, ["rusted_key"])
    library = Room("Library", "A dusty bookshelf blocks the way south. Shelves line the walls, filled with forgotten tomes.",
                   {"south": "entrance", "west": "secret_chamber"}, ["lever"],
                   {"west": {"locked": True, "key_needed": None, "unlock_msg": "A heavy stone wall blocks the way west. It seems something needs to be pulled or turned."}})
    armory = Room("Armory", "The room is dark, filled with the shapes of old weapons. A heavy iron door leads north.",
                  {"west": "entrance", "north": "treasure_room"}, ["torch"],
                  {"north": {"locked": True, "key_needed": "rusted_key", "unlock_msg": "A heavy iron door blocks the way north. It requires a rusted key."}})
    treasure = Room("Treasure Room", "A small vault lined with velvet. A riddle is inscribed on the wall.",
                    {"south": "armory"}, ["golden_coin"])
    secret = Room("Secret Chamber", "A hidden room behind a bookshelf. Dust motes dance in the faint light.",
                  {"east": "library"}, ["ancient_scroll"])
    garden = Room("Garden", "A peaceful clearing bathed in sunlight. Birds sing softly. You've found peace.",
                  {"north": "entrance"})

    rooms = {
        "entrance_hall": entrance, "library": library, "armory": armory,
        "treasure_room": treasure, "secret_chamber": secret, "garden": garden
    }
    items = {
        "rusted_key": rusted_key, "lever": lever, "torch": torch,
        "ancient_scroll": scroll, "golden_coin": golden_coin
    }

    state = GameState()
    state.rooms = rooms
    state.items = items
    state.visited_rooms.add("entrance_hall")
    return state


def main() -> None:
    """Entry point for the game engine."""
    state = create_world()
    engine = GameEngine(state)
    engine.run()


if __name__ == "__main__":
    main()
