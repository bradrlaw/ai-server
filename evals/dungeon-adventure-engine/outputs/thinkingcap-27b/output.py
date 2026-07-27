import sys
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Core Data Structures
# ---------------------------------------------------------------------------

class Item:
    """Represents an interactive object in the game world."""
    def __init__(self, item_id: str, name: str, description: str, 
                 on_use: Optional[Callable[["GameState"], str]] = None) -> None:
        self.id: str = item_id
        self.name: str = name
        self.description: str = description
        self.on_use: Optional[Callable[["GameState"], str]] = on_use


class GameState:
    """Tracks all mutable game state without relying on global variables."""
    def __init__(self) -> None:
        self.current_room: str = "entrance"
        self.inventory: list[str] = []
        self.visited_rooms: set[str] = set()
        self.flags: dict[str, bool] = {}
        self.history: list[str] = []


class Room:
    """Represents a location in the game world."""
    def __init__(self, room_id: str, name: str, description_fn: Callable[["GameState"], str],
                 exits: dict[str, str], items: list[str], 
                 locked_exits: Optional[dict[str, str]] = None) -> None:
        self.id: str = room_id
        self.name: str = name
        self.description_fn: Callable[["GameState"], str] = description_fn
        self.exits: dict[str, str] = exits
        self.items: list[str] = list(items)
        self.locked_exits: dict[str, str] = locked_exits or {}

    def get_description(self, state: GameState) -> str:
        """Returns the current room description, dynamically evaluated against game state."""
        return self.description_fn(state)


# ---------------------------------------------------------------------------
# Game Engine
# ---------------------------------------------------------------------------

# Module-level constants for parsing
VALID_COMMANDS: dict[str, str] = {
    "look": "look", "examine": "look",
    "go": "go", "move": "go",
    "take": "take", "grab": "take",
    "drop": "drop",
    "inventory": "inventory", "i": "inventory", "inv": "inventory",
    "use": "use",
    "help": "help",
    "history": "history",
    "quit": "quit", "exit": "quit"
}

VALID_DIRECTIONS: dict[str, str] = {
    "n": "north", "s": "south", "e": "east", "w": "west",
    "north": "north", "south": "south", "east": "east", "west": "west"
}


class GameEngine:
    """Main engine that manages game flow, input parsing, and state updates."""
    def __init__(self) -> None:
        self.state: GameState = GameState()
        self.items: dict[str, Item] = {}
        self.rooms: dict[str, Room] = {}
        self.running: bool = True
        self._setup_world()

    # -----------------------------------------------------------------------
    # World Setup
    # -----------------------------------------------------------------------
    def _setup_world(self) -> None:
        """Initializes all rooms, items, and their interactions."""
        # Items
        self.items["rusted_key"] = Item(
            "rusted_key", "rusted key", "A heavy iron key, covered in decades of rust.",
            on_use=lambda s: "The key is just a key. You'll need a lock for it."
        )
        self.items["lever"] = Item(
            "lever", "lever", "A sturdy iron lever protruding from the stone wall.",
            on_use=lambda s: (s.flags.update({"lever_pulled": True}) or 
                              "You pull the lever with a grunt. Somewhere, heavy stones grind and shift.")
        )
        self.items["torch"] = Item(
            "torch", "torch", "A dry pine torch, ready to be lit.",
            on_use=lambda s: "The torch burns steadily, illuminating your path."
        )
        self.items["golden_coin"] = Item(
            "golden_coin", "golden coin", "A perfectly minted coin, stamped with a forgotten king's face.",
            on_use=lambda s: "You flip the golden coin. It lands on its edge. Impressive."
        )
        self.items["ancient_scroll"] = Item(
            "ancient_scroll", "ancient scroll", "A fragile parchment inscribed with glowing runes.",
            on_use=lambda s: (s.flags.update({"won": True}) or 
                              "You unroll the ancient scroll. The text reveals the true nature of the dungeon: "
                              "It was never a prison, but a sanctuary. You have uncovered the secret ending!")
        )

        # Room Descriptions (Dynamic)
        def entrance_desc(state: GameState) -> str:
            return "You stand in the dimly lit Entrance Hall. Stone walls surround you. A draft whispers from the north."

        def library_desc(state: GameState) -> str:
            base = "You enter the dusty Library. Ancient tomes line the shelves."
            if state.flags.get("lever_pulled"):
                base += " The heavy bookshelf has shifted, revealing a hidden passage to the west."
            else:
                base += " A massive bookshelf blocks the western wall."
            return base

        def armory_desc(state: GameState) -> str:
            base = "The Armory smells of old iron and dust. Weapons hang on the walls."
            if state.flags.get("torch_taken"):
                base += " Your torch casts a warm, flickering light across the room."
            return base

        def treasure_desc(state: GameState) -> str:
            return "The Treasure Room glitters with forgotten wealth. An inscription on the wall reads: 'Wealth is but a shadow of wisdom.'"

        def secret_desc(state: GameState) -> str:
            return "The Secret Chamber is silent and cold. Ancient runes glow faintly on the floor."

        def garden_desc(state: GameState) -> str:
            return "You step into the Garden. Sunlight filters through the trees. A peaceful clearing offers a moment of respite. You've found peace."

        # Rooms
        self.rooms["entrance"] = Room(
            "entrance", "Entrance Hall", entrance_desc,
            exits={"north": "library", "east": "armory", "south": "garden"},
            items=["rusted_key"]
        )
        self.rooms["library"] = Room(
            "library", "Library", library_desc,
            exits={"south": "entrance", "west": "secret"},
            items=["lever"],
            locked_exits={"west": "flag:lever_pulled"}
        )
        self.rooms["armory"] = Room(
            "armory", "Armory", armory_desc,
            exits={"west": "entrance", "north": "treasure"},
            items=["torch"],
            locked_exits={"north": "item:rusted_key"}
        )
        self.rooms["treasure"] = Room(
            "treasure", "Treasure Room", treasure_desc,
            exits={"south": "armory"},
            items=["golden_coin"]
        )
        self.rooms["secret"] = Room(
            "secret", "Secret Chamber", secret_desc,
            exits={"east": "library"},
            items=["ancient_scroll"]
        )
        self.rooms["garden"] = Room(
            "garden", "Garden", garden_desc,
            exits={"north": "entrance"},
            items=[]
        )

        self.state.visited_rooms.add("entrance")

    # -----------------------------------------------------------------------
    # Input Parsing
    # -----------------------------------------------------------------------
    def _parse_input(self, raw: str) -> tuple[str, str]:
        """Parses raw user input into a normalized command and argument."""
        parts = raw.strip().lower().split()
        if not parts:
            return "", ""

        raw_cmd = parts[0]
        arg = " ".join(parts[1:])

        # Check for directions first
        for short, long_dir in VALID_DIRECTIONS.items():
            if raw_cmd == short or raw_cmd.startswith(short):
                return "go", long_dir

        # Check for exact command match
        if raw_cmd in VALID_COMMANDS:
            cmd = VALID_COMMANDS[raw_cmd]
            if cmd == "look" and arg.startswith("at "):
                arg = arg[3:]
            return cmd, arg

        # Check for partial command match
        matches = [c for c in VALID_COMMANDS if raw_cmd.startswith(c) or c.startswith(raw_cmd)]
        if len(matches) == 1:
            cmd = VALID_COMMANDS[matches[0]]
            if cmd == "look" and arg.startswith("at "):
                arg = arg[3:]
            return cmd, arg

        return "", ""

    def _find_item(self, query: str, state: GameState, room: Room) -> tuple[Item, str] | tuple[None, None]:
        """Searches for an item by name/ID in inventory or current room."""
        query = query.lower()
        for iid in state.inventory:
            item = self.items[iid]
            if query in item.name.lower() or query in iid:
                return item, "inventory"
        for iid in room.items:
            item = self.items[iid]
            if query in item.name.lower() or query in iid:
                return item, "room"
        return None, None

    # -----------------------------------------------------------------------
    # Command Handlers
    # -----------------------------------------------------------------------
    def _cmd_look(self, state: GameState, arg: str) -> str:
        """Handles the 'look' command."""
        if arg:
            found = self._find_item(arg, state, self.rooms[state.current_room])
            if not found:
                return "You don't see that here."
            return f"{found[0].name}: {found[0].description}"
        
        room = self.rooms[state.current_room]
        desc = room.get_description(state)
        if room.items:
            item_names = [self.items[iid].name for iid in room.items]
            desc += f"\nYou also see: {', '.join(item_names)}."
        return desc

    def _cmd_go(self, state: GameState, direction: str) -> str:
        """Handles movement commands."""
        room = self.rooms[state.current_room]
        if direction not in room.exits:
            return f"There is no exit to the {direction}."

        if direction in room.locked_exits:
            req = room.locked_exits[direction]
            if req.startswith("item:"):
                item_id = req.split(":")[1]
                if item_id in state.inventory:
                    return f"You use the {self.items[item_id].name} to unlock the door and proceed to the {direction}."
                return f"The door to the {direction} is locked. You need the {self.items[item_id].name}."
            elif req.startswith("flag:"):
                flag_name = req.split(":")[1]
                if state.flags.get(flag_name):
                    return f"You proceed to the {direction}."
                return f"The path to the {direction} is blocked."

        state.current_room = room.exits[direction]
        state.visited_rooms.add(state.current_room)
        return f"You move to the {direction}."

    def _cmd_take(self, state: GameState, item_name: str) -> str:
        """Handles taking items."""
        room = self.rooms[state.current_room]
        found = self._find_item(item_name, state, room)
        if not found:
            return "You don't see that here."
        item, loc = found
        if loc == "inventory":
            return f"You already have the {item.name}."
        
        room.items.remove(item.id)
        state.inventory.append(item.id)
        msg = f"You take the {item.name}."
        if item.id == "torch":
            state.flags["torch_taken"] = True
        return msg

    def _cmd_drop(self, state: GameState, item_name: str) -> str:
        """Handles dropping items."""
        room = self.rooms[state.current_room]
        found = self._find_item(item_name, state, room)
        if not found:
            return "You don't have that."
        item, loc = found
        if loc == "room":
            return "You don't have that."
        
        state.inventory.remove(item.id)
        room.items.append(item.id)
        return f"You drop the {item.name}."

    def _cmd_use(self, state: GameState, item_name: str) -> str:
        """Handles using items."""
        room = self.rooms[state.current_room]
        found = self._find_item(item_name, state, room)
        if not found:
            return "You don't have that."
        item, loc = found
        if loc == "room":
            return "You can't use that here. Take it first."
        
        if item.on_use:
            return item.on_use(state)
        return f"You use the {item.name}. Nothing happens."

    def _cmd_inventory(self, state: GameState, arg: str) -> str:
        """Handles inventory listing."""
        if not state.inventory:
            return "Your inventory is empty."
        names = [self.items[iid].name for iid in state.inventory]
        return f"Inventory: {', '.join(names)}"

    def _cmd_help(self, state: GameState, arg: str) -> str:
        """Displays available commands."""
        return ("Available commands:\n"
                "  look / examine [item] - Look around or at an item\n"
                "  go / move / <dir> - Move in a direction (n, s, e, w)\n"
                "  take / grab <item> - Pick up an item\n"
                "  drop <item> - Put down an item\n"
                "  use <item> - Use an item\n"
                "  inventory / i - Check your inventory\n"
                "  history - View last 10 events\n"
                "  help - Show this message\n"
                "  quit / exit - Leave the game")

    def _cmd_history(self, state: GameState, arg: str) -> str:
        """Displays the last 10 game events."""
        if not state.history:
            return "No history yet."
        return "\n".join(state.history[-10:])

    def _cmd_quit(self, state: GameState, arg: str) -> str:
        """Exits the game loop."""
        self.running = False
        return "Goodbye!"

    # -----------------------------------------------------------------------
    # Game Loop & Output
    # -----------------------------------------------------------------------
    def _get_banner(self) -> str:
        """Returns the ASCII banner displayed at startup."""
        return """
╔══════════════════════════════════════╗
║        DUNGEON OF WHISPERS           ║
║     A Text-Based Adventure           ║
╚══════════════════════════════════════╝
"""

    def run(self) -> None:
        """Executes the main game loop."""
        print(self._get_banner())
        print(self._cmd_look(self.state, ""))
        
        while self.running:
            try:
                user_input = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                print("\n\nGoodbye!")
                break

            cmd, arg = self._parse_input(user_input)
            if not cmd:
                print("\n" + "="*40)
                print("Unknown command. Type 'help' for options.")
                continue

            handler = getattr(self, f"_cmd_{cmd}", None)
            if not handler:
                print("\n" + "="*40)
                print("Unknown command. Type 'help' for options.")
                continue

            result = handler(self.state, arg)
            print("\n" + "="*40)
            print(result)
            self.state.history.append(f"{cmd} {arg}: {result}")

            if self.state.flags.get("won"):
                print("\n🎉 VICTORY! You have uncovered the secret ending! 🎉")
                self.running = False


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    engine = GameEngine()
    engine.run()
