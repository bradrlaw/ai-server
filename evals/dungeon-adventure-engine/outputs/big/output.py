"""
Dungeon of Whispers — A Text-Based Adventure Game Engine
=========================================================
A self-contained, single-file text adventure engine with no external dependencies.
Built with Python's standard library only.

Structure:
    - Item:        A game object with name, description, and optional use behavior.
    - Room:        A location with exits, items, and optional locked doors.
    - GameState:   Mutable player/world state (inventory, flags, history, etc.).
    - GameEngine:  Command parser, game-loop driver, and output formatter.

Run with:  python dungeon.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Optional, Callable, Dict, List, Set, Tuple


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class Item:
    """Represents a single item in the game world.

    Attributes:
        item_id:   Unique identifier used internally (snake_case).
        name:      Human-readable display name.
        description: Text shown when the player examines the item.
        on_use:    Optional callback invoked when the player types 'use <item>'.
                   Signature: (state: GameState, engine: GameEngine) -> str
                   Returns a message string to display to the player.
    """

    item_id: str
    name: str
    description: str
    on_use: Optional[Callable[["GameState", "GameEngine"], str]] = None


@dataclass
class Room:
    """Represents a single location in the dungeon.

    Attributes:
        room_id:      Unique identifier (snake_case).
        name:         Human-readable room title.
        description:  Base description text.
        exits:        Mapping of direction string -> target room_id.
                      e.g. {"north": "library", "east": "armory"}
        items:        List of item_ids currently present in this room.
        locked_doors: Mapping of direction -> requirement string.
                      Requirement format: "item:<item_id>" or "flag:<flag_name>".
                      e.g. {"north": "item:rusted_key", "west": "flag:lever_pulled"}
    """

    room_id: str
    name: str
    description: str
    exits: Dict[str, str] = field(default_factory=dict)
    items: List[str] = field(default_factory=list)
    locked_doors: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Game State
# ---------------------------------------------------------------------------

class GameState:
    """Encapsulates all mutable game state.

    Tracks the player's position, inventory, visited rooms, boolean flags
    for narrative events, and a running history of all events/messages.
    """

    def __init__(self) -> None:
        self.current_room: str = ""
        self.inventory: List[str] = []
        self.visited_rooms: Set[str] = set()
        self.flags: Dict[str, bool] = {}
        self.history: List[str] = []

    def set_flag(self, flag: str, value: bool = True) -> None:
        """Set a named boolean flag.

        Args:
            flag:  Flag name (e.g. "lever_pulled", "won").
            value: The boolean value to assign.
        """
        self.flags[flag] = value

    def get_flag(self, flag: str) -> bool:
        """Retrieve a boolean flag (defaults to False if unset).

        Args:
            flag: Flag name to look up.

        Returns:
            True if the flag is set, False otherwise.
        """
        return self.flags.get(flag, False)

    def add_to_history(self, message: str) -> None:
        """Append a message to the event history.

        Args:
            message: The event or system message to record.
        """
        self.history.append(message)


# ---------------------------------------------------------------------------
# Game Engine
# ---------------------------------------------------------------------------

class GameEngine:
    """Core game engine: parses input, routes commands, mutates state, prints output.

    The engine owns the world data (rooms, items) and the GameState.
    All player interaction flows through `process_command`.
    """

    # ------------------------------------------------------------------
    # Command & Direction Aliases
    # ------------------------------------------------------------------

    # Maps partial/alias tokens to canonical command tokens.
    _COMMAND_ALIASES: Dict[str, str] = {
        "look": "look",
        "l": "look",
        "examine": "look_at",
        "x": "look_at",
        "look at": "look_at",
        "go": "go",
        "move": "go",
        "walk": "go",
        "take": "take",
        "grab": "take",
        "get": "take",
        "drop": "drop",
        "inventory": "inventory",
        "inv": "inventory",
        "i": "inventory",
        "use": "use",
        "activate": "use",
        "help": "help",
        "h": "help",
        "?": "help",
        "history": "history",
        "hist": "history",
        "quit": "quit",
        "q": "quit",
        "exit": "quit",
    }

    # Maps partial/alias tokens to canonical direction names.
    _DIRECTION_ALIASES: Dict[str, str] = {
        "north": "north",
        "n": "north",
        "south": "south",
        "s": "south",
        "east": "east",
        "e": "east",
        "west": "west",
        "w": "west",
    }

    # Directions that can be typed bare (without "go"/"move" prefix).
    _BARE_DIRECTIONS: Set[str] = {"north", "south", "east", "west", "n", "s", "e", "w"}

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(
        self,
        rooms: Dict[str, Room],
        items: Dict[str, Item],
        starting_room: str,
    ) -> None:
        """Initialise the engine with world data and a fresh GameState.

        Args:
            rooms:         Mapping of room_id -> Room.
            items:         Mapping of item_id -> Item.
            starting_room: The room_id where the player begins.
        """
        self._rooms: Dict[str, Room] = rooms
        self._items: Dict[str, Item] = items
        self._state: GameState = GameState()
        self._state.current_room = starting_room
        self._state.visited_rooms.add(starting_room)
        self._running: bool = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run the main game loop until the player quits or an exception occurs.

        Prints the banner, the starting room description, then reads
        commands until 'quit' or EOF/KeyboardInterrupt.
        """
        self._print_banner()
        self._print_separator()
        print(self._describe_current_room())
        self._state.add_to_history(f"You arrive at the {self._rooms[self._state.current_room].name}.")

        while self._running:
            try:
                raw = input("\n> ")
            except EOFError:
                print("\n\nConnection closed. Goodbye, adventurer.")
                break
            except KeyboardInterrupt:
                print("\n\nYou step away from the terminal. The dungeon awaits your return.")
                break

            self.process_command(raw.strip())

        if self._state.get_flag("won"):
            self._print_victory()

    def process_command(self, raw_input: str) -> None:
        """Parse and execute a single player command.

        Args:
            raw_input: The raw string typed by the player.
        """
        if not raw_input:
            return

        tokens = raw_input.lower().split()
        command, args = self._parse_command(tokens)

        if command is None:
            self._print_separator()
            print(f"  I don't understand '{raw_input}'. Type 'help' for a command list.")
            self._state.add_to_history(f"[Unknown command: {raw_input}]")
            return

        # Dispatch
        handler = self._command_handlers.get(command)
        if handler:
            self._print_separator()
            result = handler(args)
            if result:
                print(f"  {result}")
            self._state.add_to_history(raw_input)
        else:
            self._print_separator()
            print(f"  Unknown command: {command}")

    def quit(self) -> None:
        """Signal the engine to stop the game loop."""
        self._running = False
        print("  You close your eyes. The adventure ends... for now.")
        self._state.add_to_history("[Game ended]")

    # ------------------------------------------------------------------
    # Command Parsing
    # ------------------------------------------------------------------

    def _parse_command(
        self, tokens: List[str]
    ) -> Tuple[Optional[str], List[str]]:
        """Tokenise player input into a canonical command and argument list.

        Handles:
          - Multi-word commands ("look at", "go")
          - Bare directions ("n" → go north)
          - Synonyms and partial matches via alias table

        Args:
            tokens: Lowercased, split input tokens.

        Returns:
            (canonical_command, remaining_tokens) or (None, []) if unrecognised.
        """
        # Try two-token command match (e.g. "look at sword", "go north")
        if len(tokens) >= 2:
            two_token = f"{tokens[0]} {tokens[1]}"
            if two_token in self._COMMAND_ALIASES:
                return self._COMMAND_ALIASES[two_token], tokens[2:]

        # Try single-token command match
        if tokens[0] in self._COMMAND_ALIASES:
            return self._COMMAND_ALIASES[tokens[0]], tokens[1:]

        # Bare direction: player typed just "n", "north", etc.
        if tokens[0] in self._BARE_DIRECTIONS:
            return "go", tokens[1:]

        return None, []

    def _resolve_direction(self, arg: str) -> Optional[str]:
        """Resolve a direction argument to its canonical name.

        Args:
            arg: Raw direction token (e.g. "n", "north").

        Returns:
            Canonical direction string, or None if unrecognised.
        """
        return self._DIRECTION_ALIASES.get(arg)

    def _find_item(
        self, query: str, context: List[str]
    ) -> Optional[str]:
        """Find an item in a context list by partial name match.

        Supports prefix and substring matching (case-insensitive).

        Args:
            query:   Player's item reference (e.g. "key", "rusted").
            context: List of item_ids to search within.

        Returns:
            The matched item_id, or None.
        """
        query_lower = query.lower()
        # Exact id match
        if query_lower in context:
            return query_lower
        # Name-based prefix/substring match
        for item_id in context:
            item = self._items.get(item_id)
            if item and (
                query_lower in item.name.lower()
                or item.name.lower().startswith(query_lower)
                or item_id.startswith(query_lower)
            ):
                return item_id
        return None

    # ------------------------------------------------------------------
    # Command Handlers
    # ------------------------------------------------------------------

    def _command_handlers(self) -> Dict[str, Callable[[List[str]], Optional[str]]]:
        """Build the command dispatch table.

        Returns:
            Mapping of canonical command name -> handler function.
        """
        return {
            "look": self._cmd_look,
            "look_at": self._cmd_look_at,
            "go": self._cmd_go,
            "take": self._cmd_take,
            "drop": self._cmd_drop,
            "inventory": self._cmd_inventory,
            "use": self._cmd_use,
            "help": self._cmd_help,
            "history": self._cmd_history,
            "quit": self._cmd_quit,
        }

    def _cmd_look(self, args: List[str]) -> Optional[str]:
        """Handle 'look': display current room and visible items."""
        return self._describe_current_room()

    def _cmd_look_at(self, args: List[str]) -> Optional[str]:
        """Handle 'look at <item>': display item description.

        Searches current room items and inventory.
        """
        if not args:
            return "Look at what? Specify an item."
        query = " ".join(args)
        # Search room items first, then inventory
        room_items: List[str] = list(self._rooms[self._state.current_room].items)
        item_id = self._find_item(query, room_items + self._state.inventory)
        if item_id is None:
            return f"You don't see anything called '{query}' here."
        item = self._items[item_id]
        return f"{item.name}: {item.description}"

    def _cmd_go(self, args: List[str]) -> Optional[str]:
        """Handle 'go <dir>' / bare direction: attempt to move.

        Checks for locked doors, auto-uses the rusted key if appropriate,
        and updates the current room on success.
        """
        if not args:
            return "Go where? Specify a direction (north, south, east, west)."
        direction = self._resolve_direction(args[0])
        if direction is None:
            return f"'{args[0]}' is not a valid direction. Use north, south, east, or west."

        room = self._rooms[self._state.current_room]

        # Is there an exit in this direction?
        if direction not in room.exits:
            return f"There's no path {direction} from here."

        # Check for locked door
        if direction in room.locked_doors:
            requirement = room.locked_doors[direction]
            can_pass = self._check_lock_requirement(requirement, room, direction)
            if not can_pass:
                return self._locked_door_message(requirement, room, direction)

        # Move
        target_id = room.exits[direction]
        self._state.current_room = target_id
        self._state.visited_rooms.add(target_id)
        target_room = self._rooms[target_id]
        return f"You move {direction} into the {target_room.name}.\n{target_room.description}"

    def _check_lock_requirement(
        self, requirement: str, room: Room, direction: str
    ) -> bool:
        """Evaluate whether a locked door requirement is currently satisfied.

        Supports:
          - "item:<id>"  — player must hold the item in inventory.
                            Special case: rusted_key is consumed (auto-used).
          - "flag:<name>" — the game flag must be True.

        Args:
            requirement: The requirement string from Room.locked_doors.
            room:        The room containing the locked door.
            direction:   The direction of the locked exit.

        Returns:
            True if the player may pass.
        """
        kind, value = requirement.split(":", 1)

        if kind == "item":
            if value in self._state.inventory:
                # Auto-use: remove the key (consumed)
                if value == "rusted_key":
                    self._state.inventory.remove(value)
                    room.items.append(value)  # leave key on floor beyond
                    self._state.add_to_history("The rusted key crumbles as the lock yields.")
                    print("  The rusted key turns with a satisfying click and crumbles to dust.")
                return True
            return False

        if kind == "flag":
            return self._state.get_flag(value)

        return False

    def _locked_door_message(
        self, requirement: str, room: Room, direction: str
    ) -> str:
        """Generate a helpful message explaining why a door is locked.

        Args:
            requirement: The unmet requirement string.
            room:        The current room.
            direction:   The direction of the locked exit.

        Returns:
            A player-facing explanation.
        """
        kind, value = requirement.split(":", 1)
        if kind == "item" and value == "rusted_key":
            return (
                f"The door {direction} is locked. A small rusted keyhole peers out "
                f"at you. You'll need a rusted key to open it."
            )
        if kind == "flag" and value == "lever_pulled":
            return (
                f"The passage {direction} is sealed behind a heavy bookshelf. "
                f"You need to find a way to move it."
            )
        return f"The way {direction} is blocked. You need: {value}."

    def _cmd_take(self, args: List[str]) -> Optional[str]:
        """Handle 'take <item>': pick up an item from the current room."""
        if not args:
            return "Take what?"
        query = " ".join(args)
        room = self._rooms[self._state.current_room]
        item_id = self._find_item(query, room.items)
        if item_id is None:
            return f"There's nothing here called '{query}' to take."
        room.items.remove(item_id)
        self._state.inventory.append(item_id)
        item = self._items[item_id]
        msg = f"You pick up the {item.name}."
        # Torch side-effect: update Armory description
        if item_id == "torch":
            armory = self._rooms.get("armory")
            if armory:
                armory.description = (
                    "The armory is a cramped storeroom of mismatched weapons and "
                    "crumbling armor stands. The flickering light of your torch "
                    "casts long, dancing shadows across the stone walls. "
                    "A heavy locked door blocks the passage north."
                )
            self._state.add_to_history("Torch taken. The armory is now illuminated.")
        return msg

    def _cmd_drop(self, args: List[str]) -> Optional[str]:
        """Handle 'drop <item>': drop an item from inventory into the current room."""
        if not args:
            return "Drop what?"
        query = " ".join(args)
        item_id = self._find_item(query, self._state.inventory)
        if item_id is None:
            return f"You aren't carrying '{query}'."
        self._state.inventory.remove(item_id)
        self._rooms[self._state.current_room].items.append(item_id)
        item = self._items[item_id]
        return f"You drop the {item.name} on the floor."

    def _cmd_inventory(self, args: List[str]) -> Optional[str]:
        """Handle 'inventory' / 'i': list carried items."""
        if not self._state.inventory:
            return "Your pack is empty."
        lines = ["You are carrying:"]
        for item_id in self._state.inventory:
            item = self._items[item_id]
            lines.append(f"  • {item.name}")
        return "\n".join(lines)

    def _cmd_use(self, args: List[str]) -> Optional[str]:
        """Handle 'use <item>': activate an item's on_use effect."""
        if not args:
            return "Use what?"
        query = " ".join(args)
        # Search inventory first, then room items (e.g. "use lever" in Library)
        room = self._rooms[self._state.current_room]
        item_id = self._find_item(query, self._state.inventory + room.items)
        if item_id is None:
            return f"You don't have '{query}' to use."
        item = self._items[item_id]
        if item.on_use is None:
            return f"The {item.name} doesn't seem to do anything special."
        result = item.on_use(self._state, self)
        return result

    def _cmd_help(self, args: List[str]) -> Optional[str]:
        """Handle 'help': display the command reference."""
        return (
            "Available commands:\n"
            "  look / l              — Describe the current room\n"
            "  look at <item> / x    — Examine a specific item\n"
            "  go <dir> / <dir>      — Move (north, south, east, west)\n"
            "  take <item> / grab    — Pick up an item\n"
            "  drop <item>           — Drop an item from inventory\n"
            "  inventory / i         — List your belongings\n"
            "  use <item>            — Activate an item's special effect\n"
            "  history / hist        — Show the last 10 events\n"
            "  help / h / ?          — Show this help\n"
            "  quit / q / exit       — Leave the game\n\n"
            "Tip: bare directions work too (just type 'n' to go north)."
        )

    def _cmd_history(self, args: List[str]) -> Optional[str]:
        """Handle 'history': print the last 10 logged events."""
        if not self._state.history:
            return "No events recorded yet."
        recent = self._state.history[-10:]
        lines = ["— Recent events —"]
        for i, event in enumerate(recent, 1):
            lines.append(f"  {i}. {event}")
        return "\n".join(lines)

    def _cmd_quit(self, args: List[str]) -> Optional[str]:
        """Handle 'quit': end the game gracefully."""
        self.quit()
        return None

    # ------------------------------------------------------------------
    # Output Helpers
    # ------------------------------------------------------------------

    def _print_banner(self) -> None:
        """Display the ASCII art title banner."""
        banner = (
            r"""
    ╔══════════════════════════════════════════════════════════╗
    ║                                                          ║
    ║         D U N G E O N     O F     W H I S P E R S        ║
    ║                                                          ║
    ║        A text adventure in the standard library          ║
    ║                                                          ║
    ╚══════════════════════════════════════════════════════════╝
            """
        )
        print(banner)
        print("  Type 'help' for commands, 'quit' to leave.\n")

    def _print_separator(self) -> None:
        """Print a visual separator line before each command result."""
        print("─" * 52)

    def _print_victory(self) -> None:
        """Print the final victory screen if the secret ending was achieved."""
        print("\n" + "═" * 52)
        print("  ★ YOU HAVE DISCOVERED THE SECRET ENDING ★")
        print("  The scroll's words echo through eternity.")
        print("  Congratulations, brave adventurer!")
        print("═" * 52)

    def _describe_current_room(self) -> str:
        """Build the full description string for the current room.

        Includes the room name, description text, visible items, and exits.
        Applies dynamic description changes based on game flags.

        Returns:
            Formatted multi-line room description.
        """
        room = self._rooms[self._state.current_room]
        lines: List[str] = [f"═══ {room.name} ═══", room.description]

        # Visible items in this room
        if room.items:
            item_names = [self._items[i].name for i in room.items if i in self._items]
            if item_names:
                lines.append(f"  You see: {', '.join(item_names)}.")

        # Available exits (respect locked doors visually)
        exit_parts: List[str] = []
        for direction, target_id in room.exits.items():
            if direction in room.locked_doors:
                req = room.locked_doors[direction]
                if not self._check_lock_requirement(req, room, direction):
                    exit_parts.append(f"{direction} (locked)")
                else:
                    exit_parts.append(direction)
            else:
                exit_parts.append(direction)
        if exit_parts:
            lines.append(f"  Exits: {', '.join(exit_parts)}.")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Item callback implementations (bound as on_use)
    # ------------------------------------------------------------------

    @staticmethod
    def _lever_use(state: "GameState", engine: "GameEngine") -> str:
        """Callback for the lever item: pulls the mechanism, opens passage."""
        if state.get_flag("lever_pulled"):
            return "You pull the lever again, but the bookshelf is already in place. Nothing more happens."
        state.set_flag("lever_pulled", True)
        # Remove lever from room (it's now spent)
        room = engine._rooms.get("library")
        if room and "lever" in room.items:
            room.items.remove("lever")
        # Update library description
        if room:
            room.description = (
                "The library is a vaulted chamber of crumbling shelves and "
                "dust-choked tomes. A heavy stone bookshelf has slid aside, "
                "revealing a narrow secret passage leading west. The mechanism "
                "groans with age as it settles into place."
            )
        state.add_to_history("Lever pulled. Secret passage west is now open.")
        return (
            "You yank the iron lever. With a grinding roar, the massive "
            "bookshelf slides sideways along hidden rails, revealing a "
            "dark passage that leads west."
        )

    @staticmethod
    def _scroll_use(state: "GameState", engine: "GameEngine") -> str:
        """Callback for the ancient scroll: triggers the secret ending."""
        state.set_flag("won", True)
        state.add_to_history("The ancient scroll was read. SECRET ENDING achieved.")
        return (
            "You unroll the ancient scroll. Its parchment is warm to the touch, "
            "and the ink shimmers like liquid starlight.\n\n"
            "  'You who read these words have walked where others only dared to dream.\n"
            "   The Dungeon of Whispers was never a prison — it was a test of curiosity.\n"
            "   You have passed.\n\n"
            "   The walls dissolve. The stone becomes sky. The whispers become song.\n"
            "   You step out into a world that has been waiting for you.\n\n"
            "   — The Cartographer, First Keeper of the Whispers —'\n\n"
            "The scroll crumbles to golden dust in your hands, and the chamber "
            "around you shimmers and fades like a dream upon waking."
        )

    @staticmethod
    def _coin_use(state: "GameState", engine: "GameEngine") -> str:
        """Callback for the golden coin: purely cosmetic flavor."""
        return (
            "You flip the golden coin into the air. It lands on its edge, "
            "impossibly balanced, and whispers: 'You're welcome for the entertainment.' "
            "It rolls back into your palm. Very dignified."
        )

    @staticmethod
    def _torch_use(state: "GameState", engine: "GameEngine") -> str:
        """Callback for the torch: flavor text when explicitly used."""
        return (
            "You hold the torch high. The flame gutters and steadies, "
            "pushing back the darkness a few feet further. Shadows retreat, "
            "grumbling."
        )

    @staticmethod
    def _key_use(state: "GameState", engine: "GameEngine") -> str:
        """Callback for the rusted key: informational (auto-used at doors)."""
        return (
            "You examine the rusted key. It's old, pitted with corrosion, "
            "but the bit still holds its shape. It seems designed for a "
            "specific lock. You'd need to find the right door."
        )


# ---------------------------------------------------------------------------
# World Construction
# ---------------------------------------------------------------------------

def build_dungeon() -> GameEngine:
    """Construct the 'Dungeon of Whispers' world and return a ready GameEngine.

    Creates all rooms, items, and their interconnections, then initialises
    the engine with the Entrance Hall as the starting location.

    Returns:
        A fully initialised GameEngine instance.
    """

    # -- Items ------------------------------------------------------------

    items: Dict[str, Item] = {
        "rusted_key": Item(
            item_id="rusted_key",
            name="rusted key",
            description=(
                "A small iron key, heavily corroded with orange rust. "
                "Its shape suggests a narrow, square lock. Someone dropped it here ages ago."
            ),
            on_use=GameEngine._key_use,
        ),
        "lever": Item(
            item_id="lever",
            name="iron lever",
            description=(
                "A heavy iron lever mounted in the wall, connected to a network "
                "of hidden gears behind the bookshelf. It hasn't been touched in decades."
            ),
            on_use=GameEngine._lever_use,
        ),
        "torch": Item(
            item_id="torch",
            name="torch",
            description=(
                "A sturdy oak-handled torch with a thick, waxed wick. "
                "It's unlit, but a match is tucked into the handle."
            ),
            on_use=GameEngine._torch_use,
        ),
        "golden_coin": Item(
            item_id="golden_coin",
            name="golden coin",
            description=(
                "A gleaming gold coin, impossibly clean in this dusty place. "
                "One side bears the face of a smug-looking cat. The other side "
                "reads: 'You probably won't spend this on something useful.' "
                "Very reassuring."
            ),
            on_use=GameEngine._coin_use,
        ),
        "ancient_scroll": Item(
            item_id="ancient_scroll",
            name="ancient scroll",
            description=(
                "A scroll of ancient, supple parchment sealed with a wax "
                "impression of a closed eye. The words on it seem to shift "
                "when you're not looking directly at them."
            ),
            on_use=GameEngine._scroll_use,
        ),
    }

    # -- Rooms ------------------------------------------------------------

    rooms: Dict[str, Room] = {
        "entrance_hall": Room(
            room_id="entrance_hall",
            name="Entrance Hall",
            description=(
                "You stand in a low-ceilinged hall of grey flagstone. "
                "Damp air clings to your skin. A cold draft whispers through "
                "cracks in the walls. To the north, a corridor stretches "
                "into shadow. To the east, a wider passage opens. "
                "Something metallic glints on the floor nearby."
            ),
            exits={"north": "library", "east": "armory", "south": "garden"},
            items=["rusted_key"],
            locked_doors={},
        ),
        "library": Room(
            room_id="library",
            name="Library",
            description=(
                "A vast, dim library. Towering bookshelves line every wall, "
                "their spines cracked and illegible. Dust motes dance in the "
                "faint light filtering through a high, grimy window. A massive "
                "stone bookshelf blocks the western wall — it looks like it "
                "might be more than it seems. An iron lever is mounted beside it."
            ),
            exits={"south": "entrance_hall", "west": "secret_chamber"},
            items=["lever"],
            locked_doors={"west": "flag:lever_pulled"},
        ),
        "armory": Room(
            room_id="armory",
            name="Armory",
            description=(
                "The armory is a cramped storeroom of mismatched weapons and "
                "crumbling armor stands. Rust and dried blood paint the lower "
                "walls. A heavy iron door blocks the passage north, its lock "
                "small and square. A torch leans against a rack by the wall."
            ),
            exits={"west": "entrance_hall", "north": "treasure_room"},
            items=["torch"],
            locked_doors={"north": "item:rusted_key"},
        ),
        "treasure_room": Room(
            room_id="treasure_room",
            name="Treasure Room",
            description=(
                "Beyond the iron door, a circular chamber opens. Its walls are "
                "lined with faded tapestries depicting a cartographer mapping "
                "an impossible landscape. On a velvet cushion sits a single "
                "golden coin. Above the cushion, an inscription is carved into "
                "the stone:\n"
                "  'What has a key but opens nothing?\n"
                "   What has a lock but holds nothing?\n"
                "   What has a door but leads nowhere?\n"
                "   — Answer, and the whispers shall be your song.'\n"
                "  (The answer, of course, is: a book.)"
            ),
            exits={"south": "armory"},
            items=["golden_coin"],
            locked_doors={},
        ),
        "secret_chamber": Room(
            room_id="secret_chamber",
            name="Secret Chamber",
            description=(
                "A tiny, perfectly spherical chamber. The walls are smooth "
                "black obsidian, and the air is utterly still. In the center, "
                "on a pedestal of white marble, rests a single ancient scroll. "
                "The only exit is the passage you came through to the east. "
                "A faint warmth radiates from the scroll, as if it holds a "
                "small, sleeping fire."
            ),
            exits={"east": "library"},
            items=["ancient_scroll"],
            locked_doors={},
        ),
        "garden": Room(
            room_id="garden",
            name="Garden",
            description=(
                "You step out into a peaceful clearing. Despite being deep "
                "beneath the earth, a soft, sourceless light illuminates a "
                "small garden of impossible blue flowers. A gentle breeze "
                "carries the scent of rain and fresh soil. A stone bench sits "
                "beneath a single silver-leafed tree.\n\n"
                "You've found peace."
            ),
            exits={"north": "entrance_hall"},
            items=[],
            locked_doors={},
        ),
    }

    # -- Engine -----------------------------------------------------------

    return GameEngine(rooms=rooms, items=items, starting_room="entrance_hall")


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """Instantiate the dungeon, print the banner, and enter the game loop.

    All top-level orchestration lives here; no global mutable state is used.
    """
    engine: GameEngine = build_dungeon()
    engine.run()
