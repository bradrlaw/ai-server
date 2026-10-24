"""
Text-Based Adventure Game Engine
=================================
A self-contained, production-quality adventure game engine using only
Python's standard library. Features a small dungeon world with puzzles,
items, locked doors, and a secret ending.

Run directly:  python adventure.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEPARATOR: str = "\n" + "═" * 60 + "\n"
DIRECTIONS: tuple[str, ...] = ("north", "south", "east", "west")
DIRECTION_ALIASES: dict[str, str] = {
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
    "up": "north",
    "down": "south",
}

# Maps synonym keywords to canonical commands
SYNONYMS: dict[str, str] = {
    "examine": "look at",
    "inspect": "look at",
    "check": "look at",
    "grab": "take",
    "pick up": "take",
    "get": "take",
    "hold": "take",
    "put down": "drop",
    "discard": "drop",
    "throw": "drop",
    "bag": "inventory",
    "items": "inventory",
    "gear": "inventory",
    "go": "move",
    "walk": "move",
    "travel": "move",
    "run": "move",
    "commands": "help",
    "info": "help",
    "log": "history",
    "events": "history",
    "exit": "quit",
}

ASCII_BANNER: str = r"""
╔══════════════════════════════════════════════════════════╗
║                                                        ║
║     █████╗ ██████╗ ██████╗ ███████╗██████╗ ███████╗    ║
║    ██╔══██╗██╔══██╗██╔══██╗██╔════╝██╔══██╗██╔════╝    ║
║    ███████║██████╔╝██████╔╝█████╗  ██████╔╝█████╗      ║
║    ██╔══██║██╔═══╝ ██╔═══╝ ██╔══╝  ██╔══██╗██╔══╝      ║
║    ██║  ██║██║     ██║     ███████╗██║  ██║███████╗    ║
║    ╚═╝  ╚═╝╚═╝     ╚═╝     ╚══════╝╚═╝  ╚═╝╚══════╝    ║
║                                                        ║
║          ~ The Forgotten Dungeon ~                     ║
║                                                        ║
║   Type 'help' for available commands.                  ║
║   Type 'quit' to exit at any time.                     ║
║                                                        ║
╚══════════════════════════════════════════════════════════╝
"""

VICTORY_BANNER: str = r"""
╔══════════════════════════════════════════════════════════╗
║                                                        ║
║   ★  ★  ★  ★  ★  ★  ★  ★  ★  ★  ★  ★  ★  ★  ★     ║
║                                                        ║
║   🏆  V I C T O R Y !  🏆                             ║
║                                                        ║
║   You have uncovered the ancient secret and            ║
║   escaped the dungeon with your soul intact.           ║
║   The dungeon crumbles behind you as the scroll's      ║
║   words echo into eternity...                          ║
║                                                        ║
║   ★  ★  ★  ★  ★  ★  ★  ★  ★  ★  ★  ★  ★  ★  ★     ║
║                                                        ║
╚══════════════════════════════════════════════════════════╝
"""


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class Item:
    """Represents a collectible item in the game world.

    Attributes:
        item_id: Unique identifier used for lookups.
        name: Human-readable display name.
        description: Flavor text shown when the item is examined.
        on_use: Optional callback invoked when the player uses the item.
                Receives the current GameState and returns a message string.
    """

    item_id: str
    name: str
    description: str
    on_use: Optional[Callable[[GameState], str]] = None

    def use(self, state: GameState) -> str:
        """Activate the item's special effect, if any.

        Args:
            state: The current game state to mutate.

        Returns:
            A message describing the result of using the item.
        """
        if self.on_use is not None:
            return self.on_use(state)
        return f"You fiddle with the {self.name} but nothing happens."


@dataclass
class Room:
    """Represents a location in the dungeon.

    Attributes:
        room_id: Unique identifier for the room.
        name: Human-readable room title.
        description: Base description of the room.
        exits: Mapping of direction strings to target room IDs.
        items: List of item IDs currently present in the room.
        locked_exits: Mapping of direction strings to required item IDs.
        dynamic_descriptions: Optional mapping of flag names to alternate
                              description snippets appended when the flag is set.
    """

    room_id: str
    name: str
    description: str
    exits: dict[str, str] = field(default_factory=dict)
    items: list[str] = field(default_factory=list)
    locked_exits: dict[str, str] = field(default_factory=dict)
    dynamic_descriptions: dict[str, str] = field(default_factory=dict)

    def get_description(self, state: GameState) -> str:
        """Build the room description incorporating any dynamic text.

        Args:
            state: Current game state for flag checks.

        Returns:
            The full description string for the current state.
        """
        desc = self.description
        for flag_name, snippet in self.dynamic_descriptions.items():
            if state.flags.get(flag_name, False):
                desc += "\n" + snippet
        return desc


@dataclass
class GameState:
    """Tracks all mutable state for a single playthrough.

    Attributes:
        current_room: The room_id the player currently occupies.
        inventory: Ordered list of item_ids the player is carrying.
        visited_rooms: Set of room_ids the player has entered.
        flags: Boolean flags representing world events (e.g., lever_pulled).
        history: Chronological list of all events/messages during play.
        won: Whether the player has achieved the secret ending.
    """

    current_room: str = "entrance_hall"
    inventory: list[str] = field(default_factory=list)
    visited_rooms: set[str] = field(default_factory=set)
    flags: dict[str, bool] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    won: bool = False

    def add_to_inventory(self, item_id: str) -> None:
        """Add an item to the player's inventory."""
        if item_id not in self.inventory:
            self.inventory.append(item_id)

    def remove_from_inventory(self, item_id: str) -> None:
        """Remove an item from the player's inventory."""
        if item_id in self.inventory:
            self.inventory.remove(item_id)

    def has_item(self, item_id: str) -> bool:
        """Check whether the player is carrying a given item."""
        return item_id in self.inventory

    def set_flag(self, flag: str) -> None:
        """Set a boolean world-state flag to True."""
        self.flags[flag] = True

    def get_flag(self, flag: str) -> bool:
        """Retrieve a boolean world-state flag."""
        return self.flags.get(flag, False)

    def log(self, message: str) -> None:
        """Append a message to the event history."""
        self.history.append(message)


# ---------------------------------------------------------------------------
# Game Engine
# ---------------------------------------------------------------------------


class GameEngine:
    """Core engine that parses commands, mutates state, and renders output.

    Attributes:
        state: The GameState instance for this session.
        rooms: Registry mapping room_id -> Room.
        items: Registry mapping item_id -> Item.
        running: Whether the main game loop should continue.
    """

    def __init__(self) -> None:
        """Initialize an empty engine with no world loaded."""
        self.state: GameState = GameState()
        self.rooms: dict[str, Room] = {}
        self.items: dict[str, Item] = {}
        self.running: bool = True

    # ------------------------------------------------------------------
    # World Building
    # ------------------------------------------------------------------

    def register_room(self, room: Room) -> None:
        """Register a room in the game world."""
        self.rooms[room.room_id] = room

    def register_item(self, item: Item) -> None:
        """Register an item in the game world."""
        self.items[item.item_id] = item

    # ------------------------------------------------------------------
    # Output Helpers
    # ------------------------------------------------------------------

    def print_banner(self) -> None:
        """Print the styled ASCII title banner."""
        print(ASCII_BANNER)

    def print_separator(self) -> None:
        """Print a visual separator line between outputs."""
        print(SEPARATOR)

    def output(self, message: str) -> None:
        """Print a message, log it to history, and append a separator.

        Args:
            message: The text to display to the player.
        """
        self.state.log(message)
        print(message)
        print(SEPARATOR)

    # ------------------------------------------------------------------
    # Input Parsing
    # ------------------------------------------------------------------

    def _resolve_direction(self, token: str) -> Optional[str]:
        """Attempt to resolve a token to a canonical direction.

        Args:
            token: The raw input token (lowercased).

        Returns:
            A canonical direction string, or None if not a direction.
        """
        if token in DIRECTIONS:
            return token
        if token in DIRECTION_ALIASES:
            return DIRECTION_ALIASES[token]
        # Partial match against directions
        for d in DIRECTIONS:
            if d.startswith(token):
                return d
        return None

    def _resolve_command(self, raw_input: str) -> Optional[tuple[str, list[str]]]:
        """Parse raw input into a (command, args) tuple.

        Handles synonyms, partial command matching, and bare directions.

        Args:
            raw_input: The full line of input from the player.

        Returns:
            A tuple of (canonical_command, remaining_args), or None if
            the input cannot be parsed.
        """
        tokens: list[str] = raw_input.strip().lower().split()
        if not tokens:
            return None

        # Check for multi-word synonyms first (e.g., "look at", "pick up")
        joined = " ".join(tokens)
        for synonym, canonical in SYNONYMS.items():
            if joined == synonym or joined.startswith(synonym + " "):
                remainder = joined[len(synonym):].strip()
                return (canonical, remainder.split() if remainder else [])

        first = tokens[0]

        # Single-word synonyms that map to commands
        if first in SYNONYMS:
            canonical = SYNONYMS[first]
            remainder = tokens[1:]
            return (canonical, remainder)

        # Bare direction (e.g., "n", "north", "go north")
        direction = self._resolve_direction(first)
        if direction is not None:
            return ("move", [direction])

        # Canonical command set
        canonical_commands: list[str] = [
            "look", "go", "move", "take", "drop",
            "inventory", "use", "help", "history", "quit",
        ]

        # Exact match
        if first in canonical_commands:
            return (first, tokens[1:])

        # Partial match
        matches = [c for c in canonical_commands if c.startswith(first)]
        if len(matches) == 1:
            return (matches[0], tokens[1:])
        elif len(matches) > 1:
            # Ambiguous — prefer shortest match
            matches.sort(key=len)
            return (matches[0], tokens[1:])

        return None

    def _resolve_item_ref(self, tokens: list[str]) -> Optional[str]:
        """Resolve a list of tokens to an item_id via partial matching.

        Args:
            tokens: Words after the command verb (e.g., ["rusted", "key"]).

        Returns:
            The matching item_id, or None.
        """
        if not tokens:
            return None
        query = " ".join(tokens)

        # Exact match on item_id or name
        for item_id, item in self.items.items():
            if item_id == query or item.name.lower() == query:
                return item_id

        # Partial match on item_id (tokens joined by underscore)
        for item_id, item in self.items.items():
            if item_id.startswith(query.replace(" ", "_")):
                return item_id

        # Partial match on name
        for item_id, item in self.items.items():
            if item.name.lower().startswith(query):
                return item_id

        # Word-level partial: check if all query words are prefixes of name words
        for item_id, item in self.items.items():
            name_words = item.name.lower().split()
            if len(name_words) == len(tokens):
                if all(nw.startswith(t) for nw, t in zip(name_words, tokens)):
                    return item_id

        return None

    # ------------------------------------------------------------------
    # Command Handlers
    # ------------------------------------------------------------------

    def cmd_look(self, args: list[str]) -> None:
        """Handle the 'look' and 'look at <item>' commands.

        Args:
            args: Remaining tokens after the command verb.
        """
        if args:
            # "look at <item>" — examine a specific item
            item_id = self._resolve_item_ref(args)
            if item_id is None:
                self.output(f"You don't see '{' '.join(args)}' here.")
                return
            item = self.items[item_id]
            # Item must be in current room or inventory
            room = self.rooms[self.state.current_room]
            if item_id in room.items or self.state.has_item(item_id):
                self.output(f"{item.name}: {item.description}")
            else:
                self.output(f"You don't see any '{item.name}' here.")
            return

        # Plain "look" — describe current room
        room = self.rooms[self.state.current_room]
        desc = room.get_description(self.state)
        visible_items = [
            self.items[iid].name for iid in room.items if iid in self.items
        ]
        lines: list[str] = [f"[{room.name}]", "", desc]
        if visible_items:
            lines.append("")
            lines.append("You see: " + ", ".join(visible_items) + ".")
        exit_str = ", ".join(sorted(room.exits.keys()))
        lines.append("")
        lines.append(f"Exits: {exit_str}")
        self.output("\n".join(lines))

    def cmd_move(self, args: list[str]) -> None:
        """Handle movement commands ('go', 'move', or bare direction).

        Args:
            args: Tokens containing the direction to move.
        """
        if not args:
            self.output("Go where? Specify a direction (north, south, east, west).")
            return

        direction = self._resolve_direction(args[0])
        if direction is None:
            self.output(
                f"'{args[0]}' is not a valid direction. "
                "Try north, south, east, or west."
            )
            return

        room = self.rooms[self.state.current_room]

        if direction not in room.exits:
            self.output(f"There is no exit to the {direction} here.")
            return

        # Check for locked exits
        if direction in room.locked_exits:
            required_item_id = room.locked_exits[direction]
            if self.state.has_item(required_item_id):
                # Auto-use: unlock the door
                required_name = self.items[required_item_id].name
                del room.locked_exits[direction]
                self.output(
                    f"You use the {required_name} to unlock the door to the "
                    f"{direction}. It creaks open!"
                )
                self._perform_move(direction, room)
            else:
                required_name = self.items[required_item_id].name
                self.output(
                    f"The door to the {direction} is locked. "
                    f"You need a {required_name}."
                )
            return

        self._perform_move(direction, room)

    def _perform_move(self, direction: str, current_room: Room) -> None:
        """Execute the actual room transition.

        Args:
            direction: The direction the player is moving.
            current_room: The room being left.
        """
        target_id = current_room.exits[direction]
        self.state.current_room = target_id
        self.state.visited_rooms.add(target_id)
        target_room = self.rooms[target_id]
        self.output(
            f"You move {direction} and arrive at [{target_room.name}].\n\n"
            + target_room.get_description(self.state)
        )

    def cmd_take(self, args: list[str]) -> None:
        """Handle the 'take' command.

        Args:
            args: Tokens identifying the item to pick up.
        """
        if not args:
            self.output("Take what? Specify an item.")
            return

        item_id = self._resolve_item_ref(args)
        if item_id is None:
            self.output(f"You don't see '{' '.join(args)}' here.")
            return

        room = self.rooms[self.state.current_room]
        if item_id not in room.items:
            self.output(f"There is no {self.items[item_id].name} here to take.")
            return

        room.items.remove(item_id)
        self.state.add_to_inventory(item_id)
        item = self.items[item_id]

        # Special behavior: taking the torch changes Armory description
        if item_id == "torch":
            self.state.set_flag("torch_taken")

        self.output(f"You pick up the {item.name}.")

    def cmd_drop(self, args: list[str]) -> None:
        """Handle the 'drop' command.

        Args:
            args: Tokens identifying the item to drop.
        """
        if not args:
            self.output("Drop what? Specify an item.")
            return

        item_id = self._resolve_item_ref(args)
        if item_id is None:
            self.output(f"You don't have '{' '.join(args)}'.")
            return

        if not self.state.has_item(item_id):
            self.output(f"You aren't carrying a {self.items[item_id].name}.")
            return

        self.state.remove_from_inventory(item_id)
        room = self.rooms[self.state.current_room]
        room.items.append(item_id)
        self.output(
            f"You drop the {self.items[item_id].name} on the ground."
        )

    def cmd_inventory(self, args: list[str]) -> None:
        """Handle the 'inventory' command.

        Args:
            args: Unused.
        """
        if not self.state.inventory:
            self.output("Your inventory is empty.")
            return
        item_names = [self.items[iid].name for iid in self.state.inventory]
        self.output("You are carrying:\n  • " + "\n  • ".join(item_names))

    def cmd_use(self, args: list[str]) -> None:
        """Handle the 'use' command.

        Args:
            args: Tokens identifying the item to use.
        """
        if not args:
            self.output("Use what? Specify an item.")
            return

        item_id = self._resolve_item_ref(args)
        if item_id is None:
            self.output(f"You don't have '{' '.join(args)}'.")
            return

        if not self.state.has_item(item_id):
            self.output(f"You aren't carrying a {self.items[item_id].name}.")
            return

        item = self.items[item_id]
        result = item.use(self.state)
        self.output(result)

        # Check for victory condition
        if self.state.won:
            print(VICTORY_BANNER)
            self.running = False

    def cmd_help(self, args: list[str]) -> None:
        """Handle the 'help' command.

        Args:
            args: Unused.
        """
        help_text = (
            "═══════════ AVAILABLE COMMANDS ═══════════\n\n"
            "  look              — Describe current room\n"
            "  look at <item>    — Examine an item\n"
            "  go <direction>    — Move (n/s/e/w)\n"
            "  take <item>       — Pick up an item\n"
            "  drop <item>       — Put down an item\n"
            "  inventory / i     — List carried items\n"
            "  use <item>        — Use an item's special effect\n"
            "  history           — Show last 10 events\n"
            "  help              — Show this message\n"
            "  quit              — Exit the game\n\n"
            "  Synonyms: examine=look at, grab=take, go=move\n"
            "  Directions: n, s, e, w (or full names)\n"
            "  Partial input is accepted (e.g., 'inv' → 'inventory')."
        )
        self.output(help_text)

    def cmd_history(self, args: list[str]) -> None:
        """Handle the 'history' command.

        Args:
            args: Unused.
        """
        if not self.state.history:
            self.output("No history yet.")
            return
        recent = self.state.history[-10:]
        lines = ["── Event Log (last 10) ──"]
        for i, entry in enumerate(recent, 1):
            # Truncate long entries for readability
            snippet = entry if len(entry) < 80 else entry[:77] + "..."
            lines.append(f"  {i}. {snippet}")
        self.output("\n".join(lines))

    def cmd_quit(self, args: list[str]) -> None:
        """Handle the 'quit' command.

        Args:
            args: Unused.
        """
        self.output("Thank you for playing. Farewell, adventurer!")
        self.running = False

    # ------------------------------------------------------------------
    # Command Dispatch
    # ------------------------------------------------------------------

    _COMMAND_MAP: dict[str, str] = {
        "look": "cmd_look",
        "move": "cmd_move",
        "go": "cmd_move",
        "take": "cmd_take",
        "drop": "cmd_drop",
        "inventory": "cmd_inventory",
        "use": "cmd_use",
        "help": "cmd_help",
        "history": "cmd_history",
        "quit": "cmd_quit",
    }

    def process_command(self, raw_input: str) -> None:
        """Parse and execute a single command from the player.

        Args:
            raw_input: The raw line entered by the player.
        """
        resolved = self._resolve_command(raw_input)
        if resolved is None:
            self.output(
                f"I don't understand '{raw_input.strip()}'. "
                "Type 'help' for a list of commands."
            )
            return

        command, args = resolved
        handler_name = self._COMMAND_MAP.get(command)
        if handler_name is None:
            self.output(f"Unknown command: '{command}'.")
            return

        handler = getattr(self, handler_name)
        handler(args)

    # ------------------------------------------------------------------
    # Main Game Loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run the interactive game loop until the player quits or wins."""
        self.print_banner()

        # Initial room description
        self.cmd_look([])

        while self.running:
            try:
                raw = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n\n  Farewell, adventurer. The dungeon awaits your return.")
                break

            if not raw:
                continue

            self.process_command(raw)


# ---------------------------------------------------------------------------
# Dungeon World Construction
# ---------------------------------------------------------------------------


def build_dungeon() -> GameEngine:
    """Construct and return a fully initialized GameEngine with the dungeon world.

    Returns:
        A GameEngine instance with all rooms, items, and connections registered.
    """
    engine = GameEngine()

    # --- Items -----------------------------------------------------------

    # Rusted key: opens the Armory's north door (auto-used on movement)
    rusted_key = Item(
        item_id="rusted_key",
        name="rusted key",
        description=(
            "A small, corroded iron key. It smells of old blood and "
            "forgotten promises. The teeth are worn but functional."
        ),
    )

    # Lever: unlocks the west passage in the Library when pulled
    def lever_on_use(state: GameState) -> str:
        """Pull the lever, revealing the secret passage west."""
        if state.get_flag("lever_pulled"):
            return "The lever is already in its final position. Nothing more to do."
        state.set_flag("lever_pulled")
        # Permanently unlock the west exit from the library
        library = engine.rooms["library"]
        if "west" in library.locked_exits:
            del library.locked_exits["west"]
        return (
            "You pull the lever with a grinding effort. Deep within the walls, "
            "stones shift and groan. The dusty bookshelf swings outward, "
            "revealing a narrow passage leading WEST!"
        )

    lever = Item(
        item_id="lever",
        name="lever",
        description=(
            "A heavy iron lever protruding from the wall, half-hidden behind "
            "a row of moldy encyclopedias. It hums with a faint mechanical "
            "energy, as though it has waited decades for a hand."
        ),
        on_use=lever_on_use,
    )

    # Torch: changes Armory description when taken (flag-based)
    torch = Item(
        item_id="torch",
        name="torch",
        description=(
            "A pitch-soaked torch, still dry after all these years. "
            "The wood is petrified, the binding is sinew. "
            "It would burn for hours if lit."
        ),
    )

    # Golden coin: cosmetic, witty description
    golden_coin = Item(
        item_id="golden_coin",
        name="golden coin",
        description=(
            "A gleaming gold coin stamped with the face of a king who "
            "conveniently forgot to include his name. On the reverse: "
            "'All my treasure is in another dungeon.' "
            "You suspect this is the moral of the story."
        ),
    )

    # Ancient scroll: triggers the secret ending when used
    def scroll_on_use(state: GameState) -> str:
        """Read the scroll and trigger the victory condition."""
        if state.won:
            return "You've already read the scroll. Its words are etched in your mind."
        state.won = True
        return (
            "You unfurl the ancient scroll. The ink shimmers as you read:\n\n"
            "  'The dungeon was never a prison.\n"
            "   It was a chrysalis.\n"
            "   You who read these words have already left.\n"
            "   You simply hadn't noticed.'\n\n"
            "The walls dissolve into golden light. You are free."
        )

    ancient_scroll = Item(
        item_id="ancient_scroll",
        name="ancient scroll",
        description=(
            "A scroll of impossibly thin vellum, sealed with wax the color "
            "of dried starlight. The seal bears the symbol of an eye "
            "within a keyhole. It seems to whisper when you hold it."
        ),
        on_use=scroll_on_use,
    )

    for item in (rusted_key, lever, torch, golden_coin, ancient_scroll):
        engine.register_item(item)

    # --- Rooms -----------------------------------------------------------

    entrance_hall = Room(
        room_id="entrance_hall",
        name="Entrance Hall",
        description=(
            "A vast, crumbling hall with vaulted ceilings lost in shadow. "
            "Torch sconces line the walls, long extinguished. "
            "The air tastes of dust and centuries. "
            "A rusted key lies glinting on the stone floor.\n\n"
            "Passages lead NORTH, EAST, and SOUTH."
        ),
        exits={"north": "library", "east": "armory", "south": "garden"},
        items=["rusted_key"],
    )

    library = Room(
        room_id="library",
        name="Library",
        description=(
            "Floor-to-ceiling bookshelves stretch into darkness, their "
            "contents long rotted. A thick layer of dust covers every "
            "surface. A dusty bookshelf blocks the way SOUTH, immovable "
            "by force alone. An iron lever protrudes from the wall.\n\n"
            "Exits: SOUTH (blocked), WEST (sealed)."
        ),
        exits={"south": "entrance_hall", "west": "secret_chamber"},
        items=["lever"],
        locked_exits={"west": "lever"},  # Unlocked by pulling lever, not by item
        dynamic_descriptions={
            "lever_pulled": (
                "The bookshelf hangs open, revealing a cold draft "
                "from the WEST passage. The sealed door is gone."
            ),
        },
    )

    armory = Room(
        room_id="armory",
        name="Armory",
        description=(
            "Rusted weapon racks line the walls, most empty. A few "
            "bent swords and a cracked shield remain. In the corner, "
            "a torch stands in a iron bracket.\n\n"
            "Exits: WEST, NORTH (locked door)."
        ),
        exits={"west": "entrance_hall", "north": "treasure_room"},
        items=["torch"],
        locked_exits={"north": "rusted_key"},
        dynamic_descriptions={
            "torch_taken": (
                "The empty torch bracket stands in the corner, "
                "a faint flickering light playing across the walls "
                "from some unseen source."
            ),
        },
    )

    treasure_room = Room(
        room_id="treasure_room",
        name="Treasure Room",
        description=(
            "A small, circular chamber. Most of its treasure was "
            "looted long ago, but a single golden coin rests on a "
            "pedestal of black marble. An inscription on the wall "
            "reads: 'What is taken freely is given back tenfold. "
            "What is taken by force is lost forever.'\n\n"
            "Exit: SOUTH."
        ),
        exits={"south": "armory"},
        items=["golden_coin"],
    )

    secret_chamber = Room(
        room_id="secret_chamber",
        name="Secret Chamber",
        description=(
            "A hidden room behind the bookshelf, untouched by time. "
            "Bioluminescent moss casts a pale blue glow over everything. "
            "An ancient scroll rests on a stone lectern at the center, "
            "as though placed there moments ago.\n\n"
            "Exit: EAST."
        ),
        exits={"east": "library"},
        items=["ancient_scroll"],
    )

    garden = Room(
        room_id="garden",
        name="Garden",
        description=(
            "A peaceful clearing open to the sky. Wildflowers push "
            "through cracks in ancient flagstones. A gentle stream "
            "babbles over smooth pebbles. Birds sing in a language "
            "older than the dungeon itself.\n\n"
            "Carved into a sundial at the center, a message reads:\n"
            '  "You\'ve found peace."\n\n'
            "Exit: NORTH."
        ),
        exits={"north": "entrance_hall"},
        items=[],
    )

    for room in (entrance_hall, library, armory, treasure_room, secret_chamber, garden):
        engine.register_room(room)

    # Mark the starting room as visited
    engine.state.visited_rooms.add("entrance_hall")

    return engine


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    game = build_dungeon()
    game.run()
