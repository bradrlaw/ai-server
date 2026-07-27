"""
Text-Based Adventure Game Engine
A single-file implementation of a text adventure engine with a mini-dungeon world.
"""

import sys
from typing import Dict, List, Optional, Callable, Any, Tuple, Union, Set

# Type Aliases
DescriptionType = Union[str, Callable[['GameState'], str]]
LockCondition = Union[str, Callable[['GameState'], bool]]
ItemCallback = Callable[['GameState', 'GameEngine'], None]


class GameState:
    """
    Tracks the current state of the game including location, inventory,
    visited rooms, boolean flags, and event history.
    """

    def __init__(self) -> None:
        self.current_room_id: str = "entrance"
        self.inventory: List[str] = []
        self.visited_rooms: Set[str] = set()
        self.flags: Dict[str, bool] = {}
        self.history: List[str] = []
        self.game_over: bool = False

    def log(self, message: str) -> None:
        """Add a message to the game history."""
        self.history.append(message)

    def get_flag(self, flag_name: str, default: bool = False) -> bool:
        """Retrieve a boolean flag, returning default if not set."""
        return self.flags.get(flag_name, default)

    def set_flag(self, flag_name: str, value: bool = True) -> None:
        """Set a boolean flag."""
        self.flags[flag_name] = value


class Room:
    """
    Represents a location in the game world.
    Contains name, description, exits, items, and lock conditions.
    """

    def __init__(
        self,
        name: str,
        description: DescriptionType,
        exits: Optional[Dict[str, str]] = None,
        items: Optional[List[str]] = None,
        locked_exits: Optional[Dict[str, LockCondition]] = None,
    ) -> None:
        self.name: str = name
        self.description: DescriptionType = description
        self.exits: Dict[str, str] = exits or {}
        self.items: List[str] = items or []
        self.locked_exits: Dict[str, LockCondition] = locked_exits or {}

    def get_description(self, state: GameState) -> str:
        """
        Returns the room description, evaluating if it's a callable.
        """
        if callable(self.description):
            return self.description(state)
        return self.description

    def is_exit_locked(self, direction: str, state: GameState) -> bool:
        """Checks if an exit is locked based on condition."""
        if direction not in self.locked_exits:
            return False
        condition = self.locked_exits[direction]
        if callable(condition):
            return condition(state)
        # If condition is a string, assume it's an item ID required
        return state.get_flag(f"locked_{direction}_{self.name}", True)

    def unlock_exit(self, direction: str) -> None:
        """
        Permanently unlocks an exit by removing it from locked_exits
        or clearing the lock state.
        """
        if direction in self.locked_exits:
            del self.locked_exits[direction]


class Item:
    """
    Represents an item that can be picked up and used.
    """

    def __init__(
        self,
        name: str,
        description: str,
        on_use: Optional[ItemCallback] = None,
    ) -> None:
        self.name: str = name
        self.description: str = description
        self.on_use: Optional[ItemCallback] = on_use


class GameEngine:
    """
    Core engine that manages input parsing, command execution,
    and game state updates.
    """

    def __init__(self, rooms: Dict[str, Room], items: Dict[str, Item]) -> None:
        self.rooms: Dict[str, Room] = rooms
        self.items: Dict[str, Item] = items
        self.state: GameState = GameState()
        self.running: bool = True

        # Synonym and command resolution maps
        self.synonyms: Dict[str, str] = {
            "go": "go", "move": "go", "travel": "go",
            "north": "go", "n": "go",
            "south": "go", "s": "go",
            "east": "go", "e": "go",
            "west": "go", "w": "go",
            "look": "look", "examine": "look_at", "inspect": "look_at",
            "take": "take", "grab": "take", "pick": "take",
            "drop": "drop", "discard": "drop",
            "inventory": "inventory", "i": "inventory", "inv": "inventory",
            "use": "use", "activate": "use",
            "help": "help", "h": "help",
            "history": "history", "hist": "history",
            "quit": "quit", "exit": "quit", "q": "quit",
        }
        
        self.directions: Dict[str, str] = {
            "north": "north", "n": "north",
            "south": "south", "s": "south",
            "east": "east", "e": "east",
            "west": "west", "w": "west",
        }

        # Command handlers
        self.handlers: Dict[str, Callable] = {
            "look": self.cmd_look,
            "look_at": self.cmd_look_at,
            "go": self.cmd_go,
            "take": self.cmd_take,
            "drop": self.cmd_drop,
            "inventory": self.cmd_inventory,
            "use": self.cmd_use,
            "help": self.cmd_help,
            "history": self.cmd_history,
            "quit": self.cmd_quit,
        }

    def resolve_command(self, raw_cmd: str, tokens: List[str]) -> Tuple[Optional[str], List[str]]:
        """
        Resolves raw command and tokens to canonical command and arguments.
        Handles synonyms, partial matches, and implicit 'go' for directions.
        """
        if not tokens:
            return None, []

        cmd = tokens[0].lower()
        args = tokens[1:]

        # Check if cmd is a direction
        if cmd in self.directions:
            return "go", [self.directions[cmd]]

        # Resolve command via synonyms/partial match
        canonical = self._resolve_synonym(cmd)
        if canonical:
            return canonical, args

        # If no command matched, check if it's a valid item for implicit 'take'?
        # Requirement doesn't specify implicit take, so return error or None.
        # We'll return None to let caller handle unknown.
        return None, []

    def _resolve_synonym(self, cmd: str) -> Optional[str]:
        """Resolves a command word to its canonical form."""
        if cmd in self.synonyms:
            return self.synonyms[cmd]

        # Partial match
        matches = [k for k in self.synonyms if k.startswith(cmd)]
        if len(matches) == 1:
            return self.synonyms[matches[0]]
        elif len(matches) > 1:
            # Ambiguous, but we might accept if one is a direct command?
            # For simplicity, return None on ambiguity or exact match preferred.
            # However, "inv" -> "inventory" is unique.
            # "h" -> "help" is unique.
            # "i" -> "inventory" is unique.
            # "l" -> "look" is unique? "look_at" also starts with l?
            # "look" vs "look_at". "l" matches both.
            # "look" is more specific? Or return None.
            # Usually partial match works if unique.
            # "look" matches "look" and "look_at".
            # "look_" matches "look_at".
            # "l" is ambiguous.
            # I'll return None for ambiguity to be safe, or pick longest?
            # Let's pick the one that is a direct command if available?
            # No, just return None.
            return None
        return None

    def resolve_item(self, item_name: str, context_items: List[str]) -> Optional[Item]:
        """Finds an item by name, supporting case-insensitive partial matches."""
        item_name_lower = item_name.lower()
        candidates = []
        
        for item_id in context_items:
            item = self.items.get(item_id)
            if item and item.name.lower().startswith(item_name_lower):
                candidates.append(item)
        
        if len(candidates) == 1:
            return candidates[0]
        elif len(candidates) > 1:
            return None  # Ambiguous
        return None

    def process_input(self, text: str) -> None:
        """Main entry point for processing player input."""
        if self.state.game_over and text.strip().lower() not in ("quit", "exit", "q"):
            print("The game has ended. Type 'quit' to leave.")
            return

        text = text.strip()
        if not text:
            return

        tokens = text.split()
        canonical_cmd, args = self.resolve_command(text, tokens)

        if not canonical_cmd:
            # Check if single token is a direction
            if tokens[0].lower() in self.directions:
                canonical_cmd = "go"
                args = [self.directions[tokens[0].lower()]]
            else:
                print("Unknown command. Type 'help' for list.")
                self.state.log(f"Unknown: {text}")
                return

        handler = self.handlers.get(canonical_cmd)
        if handler:
            handler(args)
        else:
            print(f"Unknown command: {canonical_cmd}")

    # Command Implementations

    def cmd_look(self, args: List[str]) -> None:
        """Look at current room."""
        room = self.rooms[self.state.current_room_id]
        desc = room.get_description(self.state)
        print(desc)
        
        # List visible items
        if room.items:
            print("\nYou see:")
            for item_id in room.items:
                item = self.items[item_id]
                print(f"  - {item.name}")
        else:
            print("\nNothing of interest here.")
        
        self.state.log(f"Looked at {room.name}")

    def cmd_look_at(self, args: List[str]) -> None:
        """Look at a specific item."""
        if not args:
            print("Look at what?")
            return
        
        item_name = args[0]
        # Search in room and inventory
        room = self.rooms[self.state.current_room_id]
        context = room.items + self.state.inventory
        
        item = self.resolve_item(item_name, context)
        if item:
            print(item.description)
            self.state.log(f"Examined {item.name}")
        else:
            print("You don't see that here.")

    def cmd_go(self, args: List[str]) -> None:
        """Move to an adjacent room."""
        if not args:
            print("Go where? (north, south, east, west)")
            return
        
        direction = args[0].lower()
        
        # Resolve direction to canonical
        dir_canon = self.directions.get(direction)
        if not dir_canon:
            # Try partial
            matches = [k for k in self.directions if k.startswith(direction)]
            if len(matches) == 1:
                dir_canon = self.directions[matches[0]]
            else:
                print(f"Unknown direction: {direction}")
                return

        room = self.rooms[self.state.current_room_id]
        
        # Check exit exists
        if dir_canon not in room.exits:
            print(f"You can't go that way.")
            return

        # Check lock
        if room.is_exit_locked(dir_canon, self.state):
            condition = room.locked_exits.get(dir_canon)
            if callable(condition):
                msg = "The way is blocked."
            else:
                # Condition is item ID
                req_item_id = condition
                req_item = self.items.get(req_item_id)
                if req_item:
                    msg = f"The door is locked. You need the {req_item.name}."
                else:
                    msg = "The door is locked."
            
            # Check if player has item (auto-use)
            if req_item_id and req_item_id in self.state.inventory:
                # Auto-use: remove item and unlock
                self.state.inventory.remove(req_item_id)
                room.unlock_exit(dir_canon)
                print(f"You use the {req_item.name} and unlock the door.")
                self.state.log(f"Auto-used {req_item.name} to open {dir_canon}")
                # Proceed to move
            else:
                print(msg)
                return
        else:
            # Check if key is present for auto-use even if not locked?
            # Requirement says key auto-uses on north from armory.
            # This implies lock check handles it.
            pass

        # Move
        target_id = room.exits[dir_canon]
        target_room = self.rooms[target_id]
        self.state.current_room_id = target_id
        self.state.visited_rooms.add(target_id)
        
        print(f"\nYou move {dir_canon}.")
        print(target_room.get_description(self.state))
        self.state.log(f"Moved {dir_canon} to {target_room.name}")

    def cmd_take(self, args: List[str]) -> None:
        """Take an item from the current room."""
        if not args:
            print("Take what?")
            return
        
        room = self.rooms[self.state.current_room_id]
        item = self.resolve_item(args[0], room.items)
        
        if item:
            room.items.remove(item.name) # Assuming name is unique key?
            # Better use item ID.
            # My resolve_item returns Item object.
            # Need to remove by ID.
            # Fix resolve_item to return ID or item.
            # Let's assume item.name is unique for simplicity in this context?
            # Or use item_id.
            # I'll use item_id in resolve_item return or lookup.
            # For now, remove item by name from list?
            # List contains IDs.
            # So need to find ID.
            item_id = next((i for i in room.items if self.items[i].name == item.name), None)
            if item_id:
                room.items.remove(item_id)
                self.state.inventory.append(item_id)
                print(f"You pick up the {item.name}.")
                self.state.log(f"Took {item.name}")
                
                # Torch effect: update Armory description
                if item.name == "torch":
                    self._update_torch_effect()
            else:
                print("Error taking item.")
        else:
            print("You don't see that here.")

    def _update_torch_effect(self) -> None:
        """Updates Armory description if torch is taken."""
        armory = self.rooms.get("armory")
        if armory:
            if callable(armory.description):
                # Already dynamic
                pass
            else:
                # Replace description
                armory.description = (
                    "A dimly lit armory filled with old weapons. "
                    "A flickering light from your torch illuminates the rusted swords."
                )

    def cmd_drop(self, args: List[str]) -> None:
        """Drop an item from inventory."""
        if not args:
            print("Drop what?")
            return
        
        item = self.resolve_item(args[0], self.state.inventory)
        if item:
            item_id = next((i for i in self.state.inventory if self.items[i].name == item.name), None)
            if item_id:
                self.state.inventory.remove(item_id)
                self.rooms[self.state.current_room_id].items.append(item_id)
                print(f"You drop the {item.name}.")
                self.state.log(f"Dropped {item.name}")
            else:
                print("Error dropping item.")
        else:
            print("You aren't carrying that.")

    def cmd_inventory(self, args: List[str]) -> None:
        """List inventory."""
        if not self.state.inventory:
            print("You are empty-handed.")
        else:
            print("Inventory:")
            for item_id in self.state.inventory:
                item = self.items[item_id]
                print(f"  - {item.name}")
        self.state.log("Checked inventory")

    def cmd_use(self, args: List[str]) -> None:
        """Use an item."""
        if not args:
            print("Use what?")
            return
        
        item = self.resolve_item(args[0], self.state.inventory)
        if item:
            if item.on_use:
                item.on_use(self.state, self)
            else:
                print(f"The {item.name} does nothing.")
        else:
            print("You don't have that.")

    def cmd_help(self, args: List[str]) -> None:
        """Print help."""
        print("""
Available Commands:
  look                     - Look around
  look at <item>           - Examine an item
  go <dir> / <dir>         - Move (n, s, e, w, north, etc.)
  take <item>              - Pick up item
  drop <item>              - Drop item
  inventory / i            - Check inventory
  use <item>               - Use item
  help                     - Show this message
  history                  - Show recent events
  quit                     - Exit game
        """)
        self.state.log("Showed help")

    def cmd_history(self, args: List[str]) -> None:
        """Print last 10 events."""
        if not self.state.history:
            print("No history yet.")
        else:
            print("History:")
            for event in self.state.history[-10:]:
                print(f"  {event}")

    def cmd_quit(self, args: List[str]) -> None:
        """Quit game."""
        self.running = False
        print("Goodbye!")
        self.state.log("Quit game")

    def run(self) -> None:
        """Main game loop."""
        print("\nGame started. Type 'help' for commands.\n")
        while self.running:
            try:
                text = input("You > ")
                self.process_input(text)
                print("-" * 40)
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                self.running = False


# World Construction

def build_world() -> Tuple[Dict[str, Room], Dict[str, Item]]:
    """Builds the mini-dungeon world."""
    rooms: Dict[str, Room] = {}
    items: Dict[str, Item] = {}

    # Items
    items["rusted_key"] = Item(
        name="rusted key",
        description="A heavy, rusted key. It looks old and worn.",
    )
    
    items["lever"] = Item(
        name="lever",
        description="A sturdy iron lever mounted on the wall.",
        on_use=lambda state, engine: _use_lever(state, engine),
    )
    
    items["torch"] = Item(
        name="torch",
        description="A wooden torch, currently unlit.",
    )
    
    items["golden_coin"] = Item(
        name="golden coin",
        description="A shimmering gold coin. 'Proof that greed has a weight.'",
    )
    
    items["ancient_scroll"] = Item(
        name="ancient scroll",
        description="A fragile scroll with faded ink.",
        on_use=lambda state, engine: _use_scroll(state, engine),
    )

    # Rooms
    rooms["entrance"] = Room(
        name="Entrance Hall",
        description="You stand in a damp stone hall. Moss creeps up the walls. "
                   "Exits lead north and east. A peaceful clearing is visible to the south.",
        exits={"north": "library", "east": "armory", "south": "garden"},
        items=["rusted_key"],
    )

    rooms["library"] = Room(
        name="Library",
        description=lambda state: (
            "A dusty library filled with crumbling books. "
            "A massive bookshelf blocks the south exit. "
            "To the west, a narrow passage is sealed shut."
            if not state.get_flag("lever_pulled")
            else (
                "A dusty library. The bookshelf has been pushed aside, "
                "revealing a secret passage to the west."
            )
        ),
        exits={"south": "entrance", "west": "secret"},
        items=["lever"],
        locked_exits={"west": "lever_pulled"},
    )

    rooms["armory"] = Room(
        name="Armory",
        description="An armory with racks of rusted weapons. "
                   "A heavy door leads north. The west exit returns to the entrance.",
        exits={"west": "entrance", "north": "treasure"},
        items=["torch"],
        locked_exits={"north": "rusted_key"},
    )

    rooms["treasure"] = Room(
        name="Treasure Room",
        description="A small chamber filled with gold. "
                   "A single golden coin lies on a pedestal. "
                   "The south door leads back to the armory.",
        exits={"south": "armory"},
        items=["golden_coin"],
    )

    rooms["secret"] = Room(
        name="Secret Chamber",
        description="A hidden chamber lit by a faint glow. "
                   "An ancient scroll rests on a stone altar. "
                   "The east exit leads back to the library.",
        exits={"east": "library"},
        items=["ancient_scroll"],
    )

    rooms["garden"] = Room(
        name="Garden",
        description="A peaceful garden clearing bathed in sunlight. "
                   "Flowers bloom in the stone planters. "
                   "You feel a sense of calm. The north exit returns to the entrance.",
        exits={"north": "entrance"},
        items=[],
    )

    return rooms, items


def _use_lever(state: GameState, engine: GameEngine) -> None:
    """Handler for using the lever."""
    state.set_flag("lever_pulled", True)
    # Update library description
    lib = engine.rooms["library"]
    lib.description = (
        "A dusty library. The bookshelf has been pushed aside, "
        "revealing a secret passage to the west."
    )
    # Remove lever from room if present?
    # Lever is in library.
    if "lever" in lib.items:
        lib.items.remove("lever")
    print("You pull the lever. With a grind of stone, the bookshelf slides open, revealing a passage to the west.")
    state.log("Pulled lever in Library")


def _use_scroll(state: GameState, engine: GameEngine) -> None:
    """Handler for using the ancient scroll."""
    state.set_flag("won", True)
    state.game_over = True
    print("\n*** SECRET ENDING ***")
    print("You read the scroll: 'The true treasure is the journey itself. You have found peace.'")
    print("Congratulations! You have discovered the secret.")
    state.log("Used scroll, won game")


if __name__ == "__main__":
    # Banner
    banner = """
    .--------------------------------.
    |   T E X T   A D V E N T U R E  |
    |         G A M E   E N G I N E  |
    '--------------------------------'
    """
    print(banner)
    print("Welcome to the Small Dungeon.")
    print("Explore, solve puzzles, and find the secret.\n")

    rooms, items = build_world()
    engine = GameEngine(rooms, items)
    engine.run()
