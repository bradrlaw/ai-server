"""
Text-Based Adventure Game Engine
=================================
A single-file, standard-library-only adventure game engine featuring
a small dungeon world with puzzles, items, and a secret ending.

Run directly:  python adventure.py
"""

from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, List, Optional, Tuple


# =============================================================================
# Data Models
# =============================================================================


class Direction(Enum):
    """Valid movement directions in the game world."""

    NORTH = auto()
    SOUTH = auto()
    EAST = auto()
    WEST = auto()
    N = auto()
    S = auto()
    E = auto()
    W = auto()

    @classmethod
    def from_string(cls, text: str) -> Optional["Direction"]:
        """Resolve a direction string (possibly abbreviated) to a Direction enum.

        Args:
            text: User-supplied direction token.

        Returns:
            The matching Direction, or None if unrecognised.
        """
        normalized = text.strip().lower()
        for member in cls:
            if member.name.lower() == normalized:
                return member
        # Partial match: "nort" -> NORTH, "ea" -> EAST, etc.
        for member in cls:
            if member.name.lower().startswith(normalized):
                return member
        return None

    def canonical(self) -> str:
        """Return the full direction name (e.g. 'north') without aliases."""
        mapping = {
            Direction.N: Direction.NORTH,
            Direction.S: Direction.SOUTH,
            Direction.E: Direction.EAST,
            Direction.W: Direction.WEST,
        }
        return mapping.get(self, self).name.lower()


@dataclass
class LockedDoor:
    """Represents a door that requires a specific item to open."""

    required_item_id: str
    message_locked: str = "The door is locked."
    message_unlocked: str = "The lock clicks open."


@dataclass
class Exit:
    """A single exit from a room in a given direction."""

    target_room_id: str
    locked_door: Optional[LockedDoor] = None


@dataclass
class Item:
    """An item that can be picked up, examined, or used.

    Attributes:
        item_id: Unique identifier for the item.
        name: Display name.
        description: Flavor text shown on 'look at'.
        on_use: Optional callback invoked when the player uses the item.
                Signature: (game_state: GameState, engine: GameEngine) -> str
    """

    item_id: str
    name: str
    description: str
    on_use: Optional[Callable[["GameState", "GameEngine"], str]] = None


@dataclass
class Room:
    """A location in the game world.

    Attributes:
        room_id: Unique identifier.
        name: Display name.
        description: Default room description.
        dynamic_description: Optional callable that overrides description
                             based on game state (e.g. torch lighting).
        exits: Mapping of Direction -> Exit.
        items: List of item_ids currently in this room.
        flags: Room-level flags (e.g. 'lever_pulled').
    """

    room_id: str
    name: str
    description: str
    dynamic_description: Optional[Callable[["GameState"], str]] = None
    exits: Dict[Direction, Exit] = field(default_factory=dict)
    items: List[str] = field(default_factory=list)
    flags: Dict[str, bool] = field(default_factory=dict)

    def get_description(self, state: "GameState") -> str:
        """Return the room description, applying dynamic overrides if present."""
        if self.dynamic_description is not None:
            return self.dynamic_description(state)
        return self.description


@dataclass
class GameState:
    """Central mutable state for a playthrough.

    Attributes:
        current_room_id: Where the player stands.
        inventory: Ordered list of item_ids the player carries.
        visited_rooms: Set of room_ids the player has entered.
        flags: Global boolean flags for story events.
        history: Chronological log of all displayed messages.
    """

    current_room_id: str = ""
    inventory: List[str] = field(default_factory=list)
    visited_rooms: set = field(default_factory=set)
    flags: Dict[str, bool] = field(default_factory=dict)
    history: List[str] = field(default_factory=list)

    def add_history(self, message: str) -> None:
        """Append a message to the event history log."""
        self.history.append(message)

    def has_item(self, item_id: str) -> bool:
        """Check whether the player holds the given item."""
        return item_id in self.inventory

    def set_flag(self, flag: str) -> None:
        """Set a global flag to True."""
        self.flags[flag] = True

    def get_flag(self, flag: str) -> bool:
        """Retrieve a global flag value (False if unset)."""
        return self.flags.get(flag, False)


# =============================================================================
# Game Engine
# =============================================================================


class GameEngine:
    """Parses player input, routes commands, manages the world, and drives the loop."""

    # Separator line printed between output blocks.
    SEPARATOR: str = "-" * 60

    # Synonym map: user token -> canonical command.
    SYNONYMS: Dict[str, str] = {
        "examine": "look",
        "inspect": "look",
        "check": "look",
        "grab": "take",
        "pick": "take",
        "get": "take",
        "grab": "take",
        "move": "go",
        "walk": "go",
        "run": "go",
        "bag": "inventory",
        "inv": "inventory",
        "i": "inventory",
        "h": "help",
        "q": "quit",
        "exit": "quit",
        "leave": "quit",
        "hist": "history",
        "log": "history",
        "l": "look",
    }

    # All valid top-level commands.
    COMMANDS: List[str] = [
        "look", "go", "take", "drop", "inventory",
        "use", "help", "history", "quit",
    ]

    def __init__(self) -> None:
        """Initialize the engine with an empty world and fresh state."""
        self.rooms: Dict[str, Room] = {}
        self.items: Dict[str, Item] = {}
        self.state: GameState = GameState()
        self.running: bool = True

    # -------------------------------------------------------------------------
    # World Building Helpers
    # -------------------------------------------------------------------------

    def add_room(self, room: Room) -> None:
        """Register a room in the game world."""
        self.rooms[room.room_id] = room

    def add_item(self, item: Item) -> None:
        """Register an item definition in the game world."""
        self.items[item.item_id] = item

    # -------------------------------------------------------------------------
    # Output Helpers
    # -------------------------------------------------------------------------

    def print_banner(self) -> None:
        """Display the ASCII art title banner at game start."""
        banner = textwrap.dedent("""\
            ╔══════════════════════════════════════════════════════╗
            ║                                                    ║
            ║     █████╗ ██╗   ██╗████████╗███████╗ ██████╗ ███╗ ██║
            ║    ██╔══██╗██║   ██║╚══██╔══╝██╔════╝██╔═══██╗████╗ ██║
            ║    ███████║██║   ██║   ██║   █████╗  ██║   ██║██╔██╗██║
            ║    ██╔══██║██║   ██║   ██║   ██╔══╝  ██║   ██║██║╚████║
            ║    ██║  ██║╚██████╔╝   ██║   ███████╗╚██████╔╝██║ ╚███║
            ║    ╚═╝  ╚═╝ ╚═════╝    ╚═╝   ╚══════╝ ╚═════╝ ╚═╝  ╚══╝
            ║                                                    ║
            ║         ⚔  A Text-Based Adventure  ⚔              ║
            ║                                                    ║
            ║   Type 'help' for commands. Type 'quit' to leave.  ║
            ╚══════════════════════════════════════════════════════╝
        """)
        print(banner)

    def print_separator(self) -> None:
        """Print the visual separator line."""
        print(self.SEPARATOR)

    def output(self, message: str) -> None:
        """Print a message, record it in history, and follow with a separator.

        Args:
            message: The text to display to the player.
        """
        print(message)
        self.state.add_history(message)

    # -------------------------------------------------------------------------
    # Input Parsing
    # -------------------------------------------------------------------------

    def parse_command(self, raw_input: str) -> Tuple[str, str]:
        """Parse raw player input into (command, argument).

        Applies synonym resolution, partial matching, and case-insensitivity.

        Args:
            raw_input: The full line typed by the player.

        Returns:
            A tuple of (canonical_command, argument_string).
            Command may be 'unknown' if parsing fails.
        """
        tokens = raw_input.strip().lower().split()
        if not tokens:
            return ("", "")

        # Check if the first token is a bare direction (shorthand for 'go <dir>')
        direction = Direction.from_string(tokens[0])
        if direction is not None and len(tokens) == 1:
            return ("go", direction.canonical())

        # Resolve command token via synonyms and partial match
        cmd_token = tokens[0]
        resolved = self._resolve_command(cmd_token)

        # Build argument from remaining tokens
        arg = " ".join(tokens[1:]) if len(tokens) > 1 else ""

        # Handle "look at <item>" -> command='look', arg='at <item>'
        if resolved == "look" and arg.lower().startswith("at "):
            arg = arg[3:]  # strip "at " prefix

        return (resolved, arg)

    def _resolve_command(self, token: str) -> str:
        """Resolve a command token to its canonical form.

        Checks synonyms first, then exact match, then partial prefix match.

        Args:
            token: Lowercased first word of input.

        Returns:
            Canonical command string, or 'unknown'.
        """
        # Direct synonym lookup
        if token in self.SYNONYMS:
            return self.SYNONYMS[token]

        # Exact command match
        if token in self.COMMANDS:
            return token

        # Partial prefix match against commands
        matches = [c for c in self.COMMANDS if c.startswith(token)]
        if len(matches) == 1:
            return matches[0]

        return "unknown"

    # -------------------------------------------------------------------------
    # Item Name Resolution
    # -------------------------------------------------------------------------

    def _find_item_by_name(self, name_query: str) -> Optional[Item]:
        """Find an item whose name contains the query string.

        Args:
            name_query: Partial or full item name typed by the player.

        Returns:
            The matching Item, or None.
        """
        query_lower = name_query.lower().strip()
        # Exact name match first
        for item in self.items.values():
            if item.name.lower() == query_lower:
                return item
        # Substring match
        for item in self.items.values():
            if query_lower in item.name.lower():
                return item
        return None

    # -------------------------------------------------------------------------
    # Command Handlers
    # -------------------------------------------------------------------------

    def cmd_look(self, arg: str) -> None:
        """Handle the 'look' command: describe room or examine an item."""
        room = self.rooms[self.state.current_room_id]

        if arg:
            # Examine a specific item
            item = self._find_item_by_name(arg)
            if item is None:
                self.output(f"You don't see '{arg}' here.")
                return
            # Verify item is in room or inventory
            in_room = item.item_id in room.items
            in_inv = item.item_id in self.state.inventory
            if not in_room and not in_inv:
                self.output(f"You don't see '{item.name}' here.")
                return
            self.output(f"[{item.name}] {item.description}")
        else:
            # Full room description
            desc = room.get_description(self.state)
            visible_items = [self.items[iid].name for iid in room.items if iid in self.items]
            exit_dirs = ", ".join(
                d.canonical() for d in room.exits
            )
            lines = [
                f"═══ {room.name} ═══",
                desc,
                "",
            ]
            if visible_items:
                lines.append(f"You can see: {', '.join(visible_items)}.")
            else:
                lines.append("You see nothing of interest.")
            lines.append(f"Exits: {exit_dirs}")
            self.output("\n".join(lines))

    def cmd_go(self, arg: str) -> None:
        """Handle movement between rooms, including locked-door logic."""
        if not arg:
            self.output("Go where? Specify a direction (north, south, east, west).")
            return

        direction = Direction.from_string(arg)
        if direction is None:
            self.output(f"'{arg}' is not a valid direction. Try north, south, east, or west.")
            return

        canonical = direction.canonical()
        room = self.rooms[self.state.current_room_id]

        # Find the exit for this direction
        exit_obj: Optional[Exit] = None
        for d, ex in room.exits.items():
            if d.canonical() == canonical:
                exit_obj = ex
                break

        if exit_obj is None:
            self.output(f"There is no exit to the {canonical} from here.")
            return

        # Handle locked door
        if exit_obj.locked_door is not None:
            door = exit_obj.locked_door
            if self.state.has_item(door.required_item_id):
                # Auto-use the key
                self.output(door.message_unlocked)
                self.state.set_flag(f"door_unlocked_{exit_obj.target_room_id}")
                # Proceed through the door
            else:
                req_name = self.items[door.required_item_id].name if door.required_item_id in self.items else door.required_item_id
                self.output(
                    f"{door.message_locked}\n"
                    f"Perhaps a '{req_name}' would help."
                )
                return

        # Move the player
        self.state.current_room_id = exit_obj.target_room_id
        self.state.visited_rooms.add(exit_obj.target_room_id)
        new_room = self.rooms[self.state.current_room_id]
        self.output(f"You move {canonical} and enter the {new_room.name}.")

        # If won flag is set, show victory after entering certain rooms
        if self.state.get_flag("won"):
            self._print_victory()

    def cmd_take(self, arg: str) -> None:
        """Handle picking up an item from the current room."""
        if not arg:
            self.output("Take what? Specify an item name.")
            return

        room = self.rooms[self.state.current_room_id]
        item = self._find_item_by_name(arg)

        if item is None:
            self.output(f"There is no '{arg}' here.")
            return

        if item.item_id not in room.items:
            self.output(f"There is no '{item.name}' here.")
            return

        room.items.remove(item.item_id)
        self.state.inventory.append(item.item_id)
        self.output(f"You pick up the {item.name}.")

    def cmd_drop(self, arg: str) -> None:
        """Handle dropping an item into the current room."""
        if not arg:
            self.output("Drop what? Specify an item name.")
            return

        item = self._find_item_by_name(arg)
        if item is None:
            self.output(f"You don't have '{arg}'.")
            return

        if not self.state.has_item(item.item_id):
            self.output(f"You're not carrying a {item.name}.")
            return

        self.state.inventory.remove(item.item_id)
        room = self.rooms[self.state.current_room_id]
        room.items.append(item.item_id)
        self.output(f"You drop the {item.name} here.")

    def cmd_inventory(self, arg: str) -> None:
        """List all items in the player's inventory."""
        if not self.state.inventory:
            self.output("Your inventory is empty.")
            return

        lines = ["═══ Inventory ═══"]
        for iid in self.state.inventory:
            if iid in self.items:
                lines.append(f"  • {self.items[iid].name}")
        self.output("\n".join(lines))

    def cmd_use(self, arg: str) -> None:
        """Handle using an item from inventory."""
        if not arg:
            self.output("Use what? Specify an item name.")
            return

        item = self._find_item_by_name(arg)
        if item is None:
            self.output(f"You don't have '{arg}'.")
            return

        if not self.state.has_item(item.item_id):
            self.output(f"You're not carrying a {item.name}.")
            return

        if item.on_use is None:
            self.output(f"There's no obvious use for the {item.name}.")
            return

        result = item.on_use(self.state, self)
        self.output(result)

        # Check for win condition after use
        if self.state.get_flag("won"):
            self._print_victory()

    def cmd_help(self, arg: str) -> None:
        """Print available commands and basic instructions."""
        help_text = textwrap.dedent("""\
            ═══ Commands ═══
              look            Describe your surroundings
              look at <item>  Examine an item closely
              go <direction>  Move (north/south/east/west)
              <direction>     Shorthand: just type 'n', 's', 'e', 'w'
              take <item>     Pick up an item
              drop <item>     Put an item down
              inventory / i   Show what you're carrying
              use <item>      Use an item from inventory
              history         Show last 10 events
              help            This message
              quit            Leave the game

            Tips:
              • Commands and items are case-insensitive.
              • Partial matches work: 'inv' = 'inventory', 'n' = 'north'.
              • Synonyms: 'examine' = 'look', 'grab' = 'take', 'move' = 'go'.
        """)
        self.output(help_text)

    def cmd_history(self, arg: str) -> None:
        """Print the last 10 recorded events."""
        recent = self.state.history[-10:]
        if not recent:
            self.output("No history yet.")
            return

        lines = ["═══ Recent History ═══"]
        for i, entry in enumerate(recent, 1):
            # Truncate long entries for readability
            short = entry.replace("\n", " ")[:80]
            lines.append(f"  {i}. {short}")
        self.output("\n".join(lines))

    def cmd_quit(self, arg: str) -> None:
        """Gracefully end the game session."""
        self.output("Thanks for playing! Farewell, adventurer. ⚔️")
        self.running = False

    # -------------------------------------------------------------------------
    # Victory
    # -------------------------------------------------------------------------

    def _print_victory(self) -> None:
        """Display the victory banner when the player wins."""
        victory = textwrap.dedent("""\

            ╔══════════════════════════════════════════════════════╗
            ║                                                    ║
            ║   🏆  V I C T O R Y !  🏆                        ║
            ║                                                    ║
            ║   You have uncovered the ancient secret and        ║
            ║   emerged victorious from the dungeon!            ║
            ║                                                    ║
            ║   The scroll's words glow with golden light,       ║
            ║   and the dungeon trembles as its magic fades.     ║
            ║   You are free.                                    ║
            ║                                                    ║
            ╚══════════════════════════════════════════════════════╝
        """)
        print(victory)

    # -------------------------------------------------------------------------
    # Command Routing
    # -------------------------------------------------------------------------

    def execute(self, raw_input: str) -> None:
        """Route a parsed command to the appropriate handler.

        Args:
            raw_input: The full player input line.
        """
        command, arg = self.parse_command(raw_input)

        if command == "":
            return  # empty input, do nothing

        handlers: Dict[str, Callable[[str], None]] = {
            "look": self.cmd_look,
            "go": self.cmd_go,
            "take": self.cmd_take,
            "drop": self.cmd_drop,
            "inventory": self.cmd_inventory,
            "use": self.cmd_use,
            "help": self.cmd_help,
            "history": self.cmd_history,
            "quit": self.cmd_quit,
        }

        handler = handlers.get(command)
        if handler is not None:
            handler(arg)
        else:
            self.output(
                f"I don't understand '{raw_input}'. Type 'help' for available commands."
            )

    # -------------------------------------------------------------------------
    # Game Loop
    # -------------------------------------------------------------------------

    def run(self) -> None:
        """Main game loop: read input, execute commands, repeat until quit."""
        self.print_banner()
        self.state.visited_rooms.add(self.state.current_room_id)

        # Initial room description
        self.cmd_look("")

        while self.running:
            try:
                user_input = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                print()  # newline after ^C / ^D
                self.output("Goodbye, adventurer. The dungeon awaits your return.")
                break

            self.print_separator()
            self.execute(user_input)


# =============================================================================
# World Construction
# =============================================================================


def build_dungeon() -> GameEngine:
    """Construct and return the full dungeon world.

    Rooms:
        - entrance   (Entrance Hall)
        - library    (Library)
        - armory     (Armory)
        - treasure   (Treasure Room)
        - secret     (Secret Chamber)
        - garden     (Garden)

    Items:
        - rusted_key, lever, torch, ancient_scroll, golden_coin

    Returns:
        A fully configured GameEngine instance ready to run.
    """
    engine = GameEngine()

    # ── Item Definitions ──────────────────────────────────────────────────

    # Rusted Key — auto-used when player tries to go north in Armory
    rusted_key = Item(
        item_id="rusted_key",
        name="rusted key",
        description=(
            "A small, corroded iron key. It smells of old blood and rust. "
            "The teeth are worn but still functional — this key opens *something*."
        ),
        on_use=None,  # Used automatically via locked-door logic
    )

    # Lever — when used in Library, unlocks the west passage
    def lever_on_use(state: GameState, eng: GameEngine) -> str:
        """Pull the lever: unlock the secret passage west from the Library."""
        if state.get_flag("lever_pulled"):
            return "The lever has already been pulled. The bookshelf stands aside."
        state.set_flag("lever_pulled")
        # Unlock the west exit in the library
        library = eng.rooms["library"]
        for d, ex in library.exits.items():
            if d.canonical() == "west":
                ex.locked_door = None  # remove lock
                break
        return (
            "You grip the cold iron lever and pull with all your might.\n"
            "A deep grinding sound echoes through the Library as the dusty "
            "bookshelf slides aside, revealing a dark passage to the west!"
        )

    lever = Item(
        item_id="lever",
        name="lever",
        description=(
            "A heavy iron lever protruding from the wall beside the bookshelf. "
            "It's covered in dust but looks sturdy. Pulling it might do something."
        ),
        on_use=lever_on_use,
    )

    # Torch — changes Armory description when held
    def torch_on_use(state: GameState, eng: GameEngine) -> str:
        """Light the torch (cosmetic flavor)."""
        if state.get_flag("torch_lit"):
            return "The torch is already burning brightly."
        state.set_flag("torch_lit")
        return "You strike a spark. The torch catches flame, casting dancing shadows on the walls."

    torch = Item(
        item_id="torch",
        name="torch",
        description=(
            "A pitch-soaked torch, still dry and ready to light. "
            "In this dank place, a source of flame could reveal hidden details."
        ),
        on_use=torch_on_use,
    )

    # Ancient Scroll — triggers the secret ending
    def scroll_on_use(state: GameState, eng: GameEngine) -> str:
        """Read the scroll: reveal the secret ending."""
        if state.get_flag("won"):
            return "You've already read the scroll. Its words are etched in your memory forever."
        state.set_flag("won")
        return (
            "You unroll the ancient scroll with trembling hands.\n"
            "The ink shimmers with an inner light as you read:\n\n"
            "    'He who seeks not gold, but knowledge;\n"
            "     who walks where others fear to tread;\n"
            "     who reads the words the dead have written —\n"
            "     shall be free of the dungeon forever.'\n\n"
            "As the last word leaves your lips, the walls dissolve into\n"
            "warm sunlight. You step out of the dungeon and into the world.\n"
            "The secret is yours."
        )

    ancient_scroll = Item(
        item_id="ancient_scroll",
        name="ancient scroll",
        description=(
            "A brittle parchment sealed with violet wax. The script upon it "
            "seems to shift and glow faintly. It whispers of a secret ending — "
            "perhaps you should 'use' it to read the words."
        ),
        on_use=scroll_on_use,
    )

    # Golden Coin — purely cosmetic, witty description
    golden_coin = Item(
        item_id="golden_coin",
        name="golden coin",
        description=(
            "A gleaming gold coin stamped with the face of a king who clearly "
            "hated having his portrait carved. On the reverse: 'THIS COIN IS "
            "NOT ACCEPTABLE AT THE DUNGEON GIFT SHOP. SORRY.' "
            "It's worth approximately 47 gold pieces, which is less than the "
            "dungeon's annual maintenance fee."
        ),
        on_use=None,
    )

    # Register items
    for item in (rusted_key, lever, torch, ancient_scroll, golden_coin):
        engine.add_item(item)

    # ── Room Definitions ──────────────────────────────────────────────────

    # Dynamic description for the Library
    def library_desc(state: GameState) -> str:
        if state.get_flag("lever_pulled"):
            return (
                "Towering bookshelves line the walls, their spines cracked and faded. "
                "A dusty bookshelf that once blocked the western wall now sits aside, "
                "revealing a dark, narrow passage leading west. The air smells of "
                "old paper and forgotten centuries."
            )
        return (
            "Towering bookshelves line the walls, their spines cracked and faded. "
            "A massive dusty bookshelf blocks the way to the west — it looks immovable. "
            "You notice a heavy iron lever mounted on the wall beside it. "
            "The air smells of old paper and forgotten centuries."
        )

    # Dynamic description for the Armory
    def armory_desc(state: GameState) -> str:
        base = (
            "Racks of rusted weapons line the walls. A suit of dented armor "
            "stands in the corner like a silent sentinel. A locked iron-banded "
            "door leads north."
        )
        if state.has_item("torch") and state.get_flag("torch_lit"):
            base += (
                "\n\nYour torch flickers, casting dancing shadows across the room. "
                "In the wavering light, you notice faint scratches on the walls — "
                "someone was here before, counting days. The locked door's "
                "keyhole gleams with a faint rust-colored shimmer."
            )
        elif state.has_item("torch"):
            base += (
                "\n\nYou clutch your torch tightly. The unlit wood is dry and "
                "smells of pitch. Perhaps lighting it would reveal more."
            )
        return base

    # Dynamic description for the Garden
    def garden_desc(state: GameState) -> str:
        return (
            "A peaceful clearing bathed in soft, impossible moonlight. "
            "Flowers you cannot name sway in a gentle breeze. A stone bench "
            "invites rest. Carved into the bench in elegant script:\n\n"
            '    "You\'ve found peace."\n\n'
            "Nothing here demands anything of you. The north path leads back."
        )

    # Entrance Hall
    entrance = Room(
        room_id="entrance",
        name="Entrance Hall",
        description=(
            "A grand but decrepit hall. Tattered banners hang from vaulted ceilings. "
            "A rusted key lies on the cold stone floor, catching a sliver of "
            "pale light. Passages lead north, east, and south."
        ),
        exits={
            Direction.NORTH: Exit(target_room_id="library"),
            Direction.EAST: Exit(target_room_id="armory"),
            Direction.SOUTH: Exit(target_room_id="garden"),
        },
        items=["rusted_key"],
    )

    # Library
    library = Room(
        room_id="library",
        name="Library",
        description="",  # Overridden by dynamic_description
        dynamic_description=library_desc,
        exits={
            Direction.SOUTH: Exit(target_room_id="entrance"),
            Direction.WEST: Exit(
                target_room_id="secret",
                locked_door=LockedDoor(
                    required_item_id="__never__",  # Never auto-opened; unlocked by lever
                    message_locked=(
                        "The bookshelf blocks the way. It won't budge. "
                        "There must be a mechanism nearby to move it."
                    ),
                ),
            ),
        },
        items=["lever"],
    )

    # Armory
    armory = Room(
        room_id="armory",
        name="Armory",
        description="",  # Overridden by dynamic_description
        dynamic_description=armory_desc,
        exits={
            Direction.WEST: Exit(target_room_id="entrance"),
            Direction.NORTH: Exit(
                target_room_id="treasure",
                locked_door=LockedDoor(
                    required_item_id="rusted_key",
                    message_locked="The iron-banded door is locked tight. You need a key.",
                    message_unlocked="You slide the rusted key into the lock. It resists, then turns with a groan.",
                ),
            ),
        },
        items=["torch"],
    )

    # Treasure Room
    treasure = Room(
        room_id="treasure",
        name="Treasure Room",
        description=(
            "A vaulted chamber glittering with faded splendor. A golden coin "
            "sits atop a stone pedestal. Behind it, an inscription is carved "
            "into the wall in an ancient tongue:\n\n"
            '    "WHAT IS SEEN IN LIGHT CANNOT BE UNSEEN."\n\n'
            "The only exit is south, back through the door you entered."
        ),
        exits={
            Direction.SOUTH: Exit(target_room_id="armory"),
        },
        items=["golden_coin"],
    )

    # Secret Chamber
    secret = Room(
        room_id="secret",
        name="Secret Chamber",
        description=(
            "A small, circular room hidden behind the bookshelf. The walls are "
            "covered in glowing violet runes. On a stone lectern rests an "
            "ancient scroll, its seal unbroken for millennia. The air hums "
            "with quiet power. An exit leads east back to the Library."
        ),
        exits={
            Direction.EAST: Exit(target_room_id="library"),
        },
        items=["ancient_scroll"],
    )

    # Garden
    garden = Room(
        room_id="garden",
        name="Garden",
        description="",  # Overridden by dynamic_description
        dynamic_description=garden_desc,
        exits={
            Direction.NORTH: Exit(target_room_id="entrance"),
        },
        items=[],
    )

    # Register rooms
    for room in (entrance, library, armory, treasure, secret, garden):
        engine.add_room(room)

    # ── Initial State ─────────────────────────────────────────────────────

    engine.state.current_room_id = "entrance"
    engine.state.visited_rooms = {"entrance"}
    engine.state.flags = {}
    engine.state.inventory = []

    return engine


# =============================================================================
# Entry Point
# =============================================================================


def main() -> None:
    """Build the dungeon world and start the game loop."""
    engine = build_dungeon()
    engine.run()


if __name__ == "__main__":
    main()
