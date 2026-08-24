"""
Text-Based Adventure Game Engine
A production-quality, single-file implementation using only the Python standard library.
"""

import sys
from typing import Dict, List, Optional, Callable, Set, Tuple


class Item:
    """Represents an interactive object in the game world."""

    def __init__(self, name: str, description: str, on_use: Optional[Callable[["GameState"], str]] = None) -> None:
        """
        Initialize an Item.

        Args:
            name: Unique identifier for the item.
            description: Text displayed when the item is examined.
            on_use: Optional callback executed when the item is used. Receives GameState and returns a message.
        """
        self.name = name
        self.description = description
        self.on_use = on_use


class Room:
    """Represents a location in the game world."""

    def __init__(
        self,
        name: str,
        description: str,
        exits: Dict[str, str],
        items: List[str],
        locked_doors: Optional[Dict[str, str]] = None
    ) -> None:
        """
        Initialize a Room.

        Args:
            name: Display name of the room.
            description: Base description of the room.
            exits: Dictionary mapping direction strings to target room IDs.
            items: List of item IDs currently in the room.
            locked_doors: Dictionary mapping direction strings to required item IDs to unlock.
        """
        self.name = name
        self.description = description
        self.exits = exits
        self.items = items
        self.locked_doors = locked_doors or {}


class GameState:
    """Tracks the mutable state of the game session."""

    def __init__(self) -> None:
        """Initialize game state with empty collections and default flags."""
        self.current_room: str = ""
        self.inventory: List[str] = []
        self.visited_rooms: Set[str] = set()
        self.flags: Dict[str, bool] = {}
        self.history: List[str] = []


class GameEngine:
    """Core engine that manages game flow, input parsing, routing, and state updates."""

    def __init__(self, rooms: Dict[str, Room], items: Dict[str, Item], start_room_id: str) -> None:
        """
        Initialize the game engine.

        Args:
            rooms: Dictionary of room ID to Room instance.
            items: Dictionary of item ID to Item instance.
            start_room_id: The ID of the room where the game begins.
        """
        self.rooms = rooms
        self.items = items
        self.state = GameState()
        self.state.current_room = start_room_id
        self.state.visited_rooms.add(start_room_id)

    def run(self) -> None:
        """Start the main game loop."""
        print(self._get_banner())
        self._cmd_look()

        try:
            while True:
                user_input = input("> ")
                print("-" * 40)
                self._handle_command(user_input)

                if self.state.flags.get("won"):
                    print("\n🎉 GAME COMPLETE! Thank you for playing.")
                    break
        except (EOFError, KeyboardInterrupt):
            print("\n\nGoodbye! Thanks for playing.")
            sys.exit(0)

    def _get_banner(self) -> str:
        """Return the ASCII art banner for the game."""
        return (
            "\n╔════════════════════════════════════════╗\n"
            "║         DUNGEON OF WHISPERS            ║\n"
            "║          A Text Adventure              ║\n"
            "╚════════════════════════════════════════╝\n"
        )

    def _log_event(self, message: str) -> None:
        """Append an event to the game history, keeping only the last 10."""
        self.state.history.append(message)
        if len(self.state.history) > 10:
            self.state.history.pop(0)

    def _find_item_by_name(self, name: str, in_room: bool) -> Optional[Item]:
        """
        Find an item by name using exact or partial matching.

        Args:
            name: The name or partial name of the item.
            in_room: If True, search room items; otherwise search inventory.

        Returns:
            The matching Item instance, or None if not found.
        """
        ids = self.rooms[self.state.current_room].items if in_room else self.state.inventory

        # Exact match first
        if name in ids:
            return self.items[name]

        # Partial match (prefix/suffix)
        for iid in ids:
            if iid.startswith(name) or name.startswith(iid):
                return self.items[iid]
        return None

    def _handle_command(self, raw: str) -> None:
        """Parse raw input, resolve synonyms/partial matches, and route to handlers."""
        tokens = raw.strip().lower().split()
        if not tokens:
            return

        cmd = tokens[0]
        args = tokens[1:]

        # Apply synonyms
        if cmd == "examine":
            cmd = "look at"
            args = tokens[1:]
        elif cmd == "grab":
            cmd = "take"
            args = tokens[1:]
        elif cmd == "i":
            cmd = "inventory"
        elif cmd == "move":
            cmd = "go"
            args = tokens[1:]

        # Direction shortcuts & normalization
        dir_map = {"n": "north", "s": "south", "e": "east", "w": "west"}
        if cmd in dir_map:
            cmd = "go"
            args = [dir_map[cmd]] if not args else [dir_map.get(args[0], args[0])]
        elif cmd == "go" and args:
            args[0] = dir_map.get(args[0], args[0])

        # Handle "look at" explicitly
        if cmd == "look" and args and args[0] == "at":
            cmd = "look at"
            args = args[1:]

        # Resolve partial command matches
        base_cmds = ["look", "go", "take", "drop", "inventory", "use", "help", "history", "quit"]
        resolved = cmd
        for bc in base_cmds:
            if cmd == bc or cmd.startswith(bc) or bc.startswith(cmd):
                resolved = bc
                break

        # Dispatch to appropriate handler
        if resolved == "look at" and args:
            self._cmd_look_at(args)
        elif resolved == "look":
            self._cmd_look()
        elif resolved == "go":
            self._cmd_go(args[0] if args else None)
        elif resolved == "take" and args:
            self._cmd_take(args)
        elif resolved == "drop" and args:
            self._cmd_drop(args)
        elif resolved == "inventory":
            self._cmd_inventory()
        elif resolved == "use" and args:
            self._cmd_use(args)
        elif resolved == "help":
            self._cmd_help()
        elif resolved == "history":
            self._cmd_history()
        elif resolved == "quit":
            self._cmd_quit()
        else:
            print("Unknown command. Type 'help' for available commands.")

    def _cmd_look(self) -> None:
        """Display the current room description and visible items."""
        room = self.rooms[self.state.current_room]
        desc = room.description

        # Dynamic description updates
        if self.state.current_room == "armory" and "torch" in self.state.inventory:
            desc += " The torch flickers, casting long shadows across the weapons."
        if self.state.current_room == "library" and self.state.flags.get("lever_pulled"):
            desc += " The dusty bookshelf has been moved, revealing a secret passage to the west."

        print(f"\n{room.name}")
        print(desc)

        if room.items:
            item_names = [self.items[iid].name for iid in room.items]
            print(f"You see: {', '.join(item_names)}")

        self._log_event(f"Looked around {room.name}")

    def _cmd_look_at(self, args: List[str]) -> None:
        """Display the description of a specific item."""
        target_name = " ".join(args)
        item = self._find_item_by_name(target_name, in_room=True) or self._find_item_by_name(target_name, in_room=False)

        if item:
            print(f"{item.name}: {item.description}")
            self._log_event(f"Examined {item.name}")
        else:
            print(f"You don't see a '{target_name}' here.")

    def _cmd_go(self, direction: Optional[str]) -> None:
        """Move the player to an adjacent room, handling locks and auto-use."""
        if not direction:
            print("Where do you want to go?")
            return

        room = self.rooms[self.state.current_room]

        if direction not in room.exits:
            print("You can't go that way.")
            return

        # Check for locked doors
        if direction in room.locked_doors:
            req_item_id = room.locked_doors[direction]
            if req_item_id in self.state.inventory:
                req_item_name = self.items[req_item_id].name
                if req_item_id == "rusted key":
                    print(f"You use the {req_item_name} to unlock the door.")
                    self._log_event(f"Unlocked {direction} door with {req_item_name}")
                # Remove lock permanently
                del room.locked_doors[direction]
            else:
                print(f"The door to the {direction} is locked. You need a {self.items[req_item_id].name}.")
                return

        # Execute movement
        target_id = room.exits[direction]
        self.state.current_room = target_id
        self.state.visited_rooms.add(target_id)
        self._cmd_look()
        self._log_event(f"Moved {direction} to {self.rooms[target_id].name}")

    def _cmd_take(self, args: List[str]) -> None:
        """Pick up an item from the current room."""
        target_name = " ".join(args)
        item = self._find_item_by_name(target_name, in_room=True)

        if not item:
            print(f"You don't see a '{target_name}' here.")
            return

        if item.name in self.state.inventory:
            print(f"You already have the {item.name}.")
            return

        room = self.rooms[self.state.current_room]
        room.items.remove(item.name)
        self.state.inventory.append(item.name)

        # Special behavior for torch
        if item.name == "torch":
            self.rooms["armory"].description += " The torch flickers, casting long shadows."
            print("You pick up the torch. Its flame illuminates the dark corners.")
        else:
            print(f"Taken: {item.name}")

        self._log_event(f"Took {item.name}")

    def _cmd_drop(self, args: List[str]) -> None:
        """Drop an item from inventory into the current room."""
        target_name = " ".join(args)
        item = self._find_item_by_name(target_name, in_room=False)

        if not item:
            print(f"You aren't carrying a '{target_name}'.")
            return

        self.state.inventory.remove(item.name)
        self.rooms[self.state.current_room].items.append(item.name)
        print(f"Dropped: {item.name}")
        self._log_event(f"Dropped {item.name}")

    def _cmd_inventory(self) -> None:
        """List all items currently in the player's inventory."""
        if not self.state.inventory:
            print("Your inventory is empty.")
        else:
            item_names = [self.items[iid].name for iid in self.state.inventory]
            print(f"Inventory: {', '.join(item_names)}")
        self._log_event("Checked inventory")

    def _cmd_use(self, args: List[str]) -> None:
        """Activate an item's special effect."""
        target_name = " ".join(args)
        item = self._find_item_by_name(target_name, in_room=False)

        if not item:
            print(f"You aren't carrying a '{target_name}'.")
            return

        if item.on_use:
            result_msg = item.on_use(self.state)
            print(result_msg)
            self._log_event(f"Used {item.name}")
        else:
            print(f"You can't use the {item.name} here.")

    def _cmd_help(self) -> None:
        """Print available commands and usage instructions."""
        print("""
Available Commands:
  look              - Describe the current room and visible items
  look at <item>    - Examine a specific item
  go <direction>    - Move north, south, east, or west (aliases: n, s, e, w, move)
  take <item>       - Pick up an item (alias: grab)
  drop <item>       - Leave an item in the room
  inventory / i     - List carried items
  use <item>        - Activate an item's effect
  history           - Show the last 10 events
  help              - Show this message
  quit              - Exit the game
""")
        self._log_event("Viewed help")

    def _cmd_history(self) -> None:
        """Print the last 10 logged events."""
        if not self.state.history:
            print("No events recorded yet.")
        else:
            print("Recent History:")
            for i, event in enumerate(self.state.history, 1):
                print(f"  {i}. {event}")
        self._log_event("Viewed history")

    def _cmd_quit(self) -> None:
        """Gracefully exit the game."""
        print("\nThank you for playing. Goodbye!")
        sys.exit(0)


def create_world() -> Tuple[Dict[str, Room], Dict[str, Item]]:
    """Build and return the initial game world with rooms and items."""

    # Define item callbacks
    def lever_callback(state: GameState) -> str:
        state.flags["lever_pulled"] = True
        return "You pull the lever. With a heavy grinding sound, the bookshelf slides aside, revealing a secret passage to the west."

    def scroll_callback(state: GameState) -> str:
        state.flags["won"] = True
        return "The ancient runes glow with brilliant light! The secrets of the dungeon are revealed to you. You have found the secret ending!"

    # Create items
    items: Dict[str, Item] = {
        "rusted key": Item("rusted key", "A heavy, iron key covered in rust. It looks old but sturdy."),
        "lever": Item("lever", "A rusted iron lever protruding from the wall. It looks like it hasn't been touched in centuries.", on_use=lever_callback),
        "torch": Item("torch", "A wooden stick wrapped in cloth, ready to be lit."),
        "golden coin": Item("golden coin", "A gleaming coin stamped with an ancient crest. It probably buys you a very small sandwich in the modern economy."),
        "ancient scroll": Item("ancient scroll", "A fragile parchment inscribed with glowing runes. It hums with latent power.", on_use=scroll_callback),
    }

    # Create rooms
    rooms: Dict[str, Room] = {
        "entrance": Room(
            "Entrance Hall",
            "You stand in a dimly lit entrance hall. Stone walls surround you, and the air smells of damp earth.",
            {"north": "library", "east": "armory", "south": "garden"},
            ["rusted key"]
        ),
        "library": Room(
            "Library",
            "Rows of ancient books line the walls. A massive dusty bookshelf blocks the way south. To the west, a dark passage is hidden behind it.",
            {"south": "entrance", "west": "secret_chamber"},
            ["lever"],
            locked_doors={"west": "lever"}
        ),
        "armory": Room(
            "Armory",
            "Racks of forgotten weapons line the walls. A heavy iron door stands to the north.",
            {"west": "entrance", "north": "treasure_room"},
            ["torch"],
            locked_doors={"north": "rusted key"}
        ),
        "treasure_room": Room(
            "Treasure Room",
            "A small vault lined with gold and jewels. An inscription on the wall reads: 'Wealth is fleeting, but knowledge endures.'",
            {"south": "armory"},
            ["golden coin"]
        ),
        "secret_chamber": Room(
            "Secret Chamber",
            "A hidden alcove filled with dust and forgotten artifacts. A pedestal stands in the center.",
            {"east": "library"},
            ["ancient scroll"]
        ),
        "garden": Room(
            "Garden",
            "A peaceful clearing bathed in soft sunlight. Birds chirp melodiously. You've found peace.",
            {"north": "entrance"},
            []
        ),
    }

    return rooms, items


def main() -> None:
    """Instantiate the world and run the game engine."""
    rooms, items = create_world()
    engine = GameEngine(rooms, items, start_room_id="entrance")
    engine.run()


if __name__ == "__main__":
    main()
