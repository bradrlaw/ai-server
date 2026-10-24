#!/usr/bin/env python3
"""A text-based adventure game engine with a small dungeon to play in.

Features:
    * ``GameState``  -- mutable world/player state (room, inventory, flags, history).
    * ``Room``       -- a location with exits, items, and optionally locked doors.
    * ``Item``       -- a pickup with an optional ``on_use`` callback.
    * ``GameEngine`` -- ties the world together: parses input, routes commands,
      mutates state, and prints output.

Run ``python output.py`` to play.  Uses only the Python standard library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

UseCallback = Callable[["GameState"], Optional[str]]


@dataclass
class DoorLock:
    """Describes the condition required to pass through a locked exit.

    Attributes:
        required_item: Item id the player must carry (checked first).
        required_flag: Game flag that must be true (checked second).
        locked_message: Message shown when the door is still locked.
        unlock_message: Message shown the first time the door opens.
    """

    required_item: Optional[str] = None
    required_flag: Optional[str] = None
    locked_message: str = "The way is blocked."
    unlock_message: str = "The door swings open."


@dataclass
class Item:
    """A game item that can be picked up, examined, and possibly used.

    Attributes:
        name: Canonical, lowercase item id (also used for matching input).
        display_name: Human-friendly name shown to the player.
        description: Text printed by ``look at <item>``.
        on_use: Optional callback invoked by ``use <item>``.  May return a
            message to print; returning ``None`` prints nothing extra.
    """

    name: str
    display_name: str
    description: str
    on_use: Optional[UseCallback] = None


@dataclass
class Room:
    """A location in the game world.

    Attributes:
        name: Canonical, lowercase room id.
        display_name: Human-friendly room name.
        description: Text printed by ``look`` (may be mutated at runtime).
        exits: Mapping of direction -> target room id.
        items: List of item ids currently lying in this room.
        locked_exits: Mapping of direction -> :class:`DoorLock`.  A direction
            may appear in both ``exits`` and ``locked_exits``; the lock is
            checked before the move is allowed.
    """

    name: str
    display_name: str
    description: str
    exits: Dict[str, str] = field(default_factory=dict)
    items: List[str] = field(default_factory=list)
    locked_exits: Dict[str, DoorLock] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Game state
# ---------------------------------------------------------------------------


class GameState:
    """All mutable player/world state for a single game session.

    Attributes:
        current_room: Id of the room the player is standing in.
        inventory: List of item ids carried by the player.
        visited: List of room ids the player has entered (in order).
        flags: Arbitrary boolean event flags (e.g. ``"won"``,
            ``"lever_pulled"``).
        history: Chronological list of every event/message printed to the
            player (used by the ``history`` command).
    """

    def __init__(self, starting_room: str) -> None:
        self.current_room: str = starting_room
        self.inventory: List[str] = []
        self.visited: List[str] = [starting_room]
        self.flags: Dict[str, bool] = {}
        self.history: List[str] = []

    def set_flag(self, name: str, value: bool = True) -> None:
        """Set a boolean event flag by name."""
        self.flags[name] = value

    def has_flag(self, name: str) -> bool:
        """Return ``True`` if the named flag is set."""
        return bool(self.flags.get(name, False))

    def record(self, message: str) -> None:
        """Append an event message to the game history."""
        self.history.append(message)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

# Canonical direction names and accepted abbreviations.
DIRECTIONS: Dict[str, str] = {
    "north": "north", "n": "north",
    "south": "south", "s": "south",
    "east": "east", "e": "east",
    "west": "west", "w": "west",
}

# One-word commands accepted as-is (case-insensitive, prefix-matched).
COMMANDS: List[str] = [
    "look", "go", "move", "take", "drop", "use",
    "inventory", "help", "history", "quit",
]

# Synonym command table: alias -> canonical command tuple.
SYNONYMS: Dict[str, List[str]] = {
    "examine": ["look"],
    "grab": ["take"],
    "get": ["take"],
    "i": ["inventory"],
    "inv": ["inventory"],
    "exit": ["quit"],
    "q": ["quit"],
    "quit": ["quit"],
}


class GameEngine:
    """Parses player input, mutates :class:`GameState`, and prints output.

    The engine owns the world (rooms and items) so that no global mutable
    state is needed; a fresh engine is a fresh game.
    """

    BANNER: str = r"""
    ___________________________________________________________
   /                                                            \
  |   _____  __   __ _____ ____  ______                         |
  |  /  _  |/  | /  //  _ // __ \/  _/ |                         |
  |  |  _  |   ||  |/  _  \ \  \//   / |                         |
  |  |______/_|__|__/______/\__\___//____/                        |
  |                                                              |
  |    ~ a text adventure in six rooms and one secret ~          |
  |______________________________________________________________|
"""

    def __init__(self) -> None:
        self.rooms: Dict[str, Room] = {}
        self.items: Dict[str, Item] = {}
        self._build_world()
        self.state = GameState(starting_room="entrance")

    # -- world construction -------------------------------------------------

    def _add_room(self, room: Room) -> None:
        """Register a room in the world."""
        self.rooms[room.name] = room

    def _add_item(self, item: Item) -> None:
        """Register an item in the world."""
        self.items[item.name] = item

    def _build_world(self) -> None:
        """Build the mini-dungeon: six rooms, five items, two locked doors."""

        # -- Items ----------------------------------------------------------

        rusted_key = Item(
            name="rusted_key",
            display_name="a rusted key",
            description=(
                "A small, corroded iron key. Its head is shaped like a "
                "crab, and it smells faintly of brine."
            ),
        )

        lever = Item(
            name="lever",
            display_name="a lever",
            description=(
                "A heavy iron lever set into a hidden panel behind the "
                "bookshelf. Something mechanical hums behind the wall."
            ),
            on_use=self._use_lever,
        )

        torch = Item(
            name="torch",
            display_name="a torch",
            description=(
                "A pitch-soaked torch, already half alight. It throws a "
                "steady, warm glow."
            ),
        )

        scroll = Item(
            name="scroll",
            display_name="an ancient scroll",
            description=(
                "A scroll of impossibly old parchment. The ink glows faintly, "
                "as if it still remembers being written."
            ),
            on_use=self._use_scroll,
        )

        coin = Item(
            name="coin",
            display_name="a golden coin",
            description=(
                "A golden coin, still warm. Someone has scratched a tiny "
                "smiley face on the tail -- whoever minted this clearly "
                "had a sense of humor, or a very bad coin press."
            ),
        )

        for item in (rusted_key, lever, torch, scroll, coin):
            self._add_item(item)

        # -- Rooms ----------------------------------------------------------

        self._add_room(Room(
            name="entrance",
            display_name="Entrance Hall",
            description=(
                "A vaulted hall of cracked flagstones. Damp air drifts up "
                "from below, and the far wall is choked with moss."
            ),
            exits={"north": "library", "east": "armory", "south": "garden"},
            items=["rusted_key"],
        ))

        self._add_room(Room(
            name="library",
            display_name="The Library",
            description=(
                "Towering shelves sag under the weight of forgotten books. "
                "A dusty bookshelf blocks the way south, and the western "
                "wall hides something behind a painted panel."
            ),
            exits={"south": "entrance", "west": "secret_chamber"},
            items=["lever"],
            locked_exits={
                "west": DoorLock(
                    required_flag="lever_pulled",
                    locked_message=(
                        "The painted panel does not budge. Behind it, a "
                        "spring-loaded mechanism is waiting to be "
                        "triggered."
                    ),
                    unlock_message=(
                        "With a deep grind of stone, the painted panel "
                        "slides aside, revealing a secret passage west."
                    ),
                )
            },
        ))

        self._add_room(Room(
            name="armory",
            display_name="The Armory",
            description=(
                "Racks of rusted swords and dented shields line the walls. "
                "A heavy iron door bars the way north."
            ),
            exits={"west": "entrance", "north": "treasure"},
            items=["torch"],
            locked_exits={
                "north": DoorLock(
                    required_item="rusted_key",
                    locked_message=(
                        "The iron door is locked tight. A small, crab-"
                        "shaped keyhole gleams in the lock."
                    ),
                    unlock_message=(
                        "The rusted key turns with a satisfying click. "
                        "The iron door grinds open."
                    ),
                )
            },
        ))

        self._add_room(Room(
            name="treasure",
            display_name="The Treasure Room",
            description=(
                "A round chamber whose walls are lined with empty velvet "
                "cushions -- the treasure long since hauled away. A riddle "
                "is carved above the south door: 'What is found but never "
                "kept, and kept but never found?'"
            ),
            exits={"south": "armory"},
            items=["coin"],
        ))

        self._add_room(Room(
            name="secret_chamber",
            display_name="The Secret Chamber",
            description=(
                "A tiny, dry chamber behind the library's painted panel. "
                "A single brazier, unlit, stands beside a pedestal."
            ),
            exits={"east": "library"},
            items=["scroll"],
        ))

        self._add_room(Room(
            name="garden",
            display_name="The Garden",
            description=(
                "A peaceful clearing of soft grass and pale stones. "
                "Somewhere far above, birds are singing. You've found "
                "peace."
            ),
            exits={"north": "entrance"},
        ))

    # -- item on_use callbacks ----------------------------------------------

    def _use_lever(self, state: GameState) -> Optional[str]:
        """Pull the lever: unlock the Library's west passage permanently."""
        room = self.rooms["library"]
        if state.current_room != room.name:
            return "You can only reach the lever from inside the Library."
        if state.has_flag("lever_pulled"):
            return "The lever is already pulled. The mechanism is silent."
        state.set_flag("lever_pulled")
        room.description = (
            "Towering shelves sag under the weight of forgotten books. "
            "A dusty bookshelf blocks the way south. To the west, a "
            "secret passage gapes open in the stone, lit by a faint "
            "inner glow."
        )
        return "You pull the lever. Somewhere deep in the walls, " \
               "counterweights shudder and a section of wall slides away."

    def _use_scroll(self, state: GameState) -> Optional[str]:
        """Unroll the ancient scroll: reveal the secret ending."""
        if state.has_flag("won"):
            return "You've already read the scroll. Its secret is yours."
        state.set_flag("won")
        state.record("The secret ending has been revealed!")
        return (
            "\n"
            "        ==============================================\n"
            "        THE SECRET ENDING\n"
            "        ==============================================\n"
            "The scroll unrolls in your hands, and its glowing ink "
            "rearranges itself into a single line:\n"
            "\n"
            "   'The dungeon was never a prison. It is a letter,\n"
            "    written by the last keeper to whoever finally\n"
            "    found their way here. You have read it now.\n"
            "    The door behind you is the same as the one\n"
            "    ahead: you may leave whenever you wish.'\n"
            "\n"
            "  *** VICTORY ***\n"
            "  You have uncovered the dungeon's deepest secret.\n"
            "  The game bows to you. Type 'quit' to step out of the "
            "light.\n"
        )

    # -- public helpers -------------------------------------------------------

    def banner(self) -> None:
        """Print the styled ASCII start banner and welcome text."""
        print(self.BANNER)
        welcome = (
            "Type 'help' for a list of commands.\n"
            "You can use abbreviations: 'n' for north, 'inv' for "
            "inventory, 'examine' for look at, 'grab' for take..."
        )
        print(welcome)
        self.state.record(welcome)

    # -- input parsing ----------------------------------------------------------

    def _parse_command(self, raw: str) -> List[str]:
        """Normalize raw input into a list of lowercase tokens."""
        return raw.strip().lower().split()

    def _resolve_command(self, tokens: List[str]) -> Optional[List[str]]:
        """Resolve a token list to a canonical command.

        Handles exact commands, synonyms (``examine`` -> ``look at``,
        ``grab`` -> ``take``), and unique prefix matches (``inv`` ->
        ``inventory``).  Returns the canonical command tokens, or
        ``None`` if nothing matches.
        """
        if not tokens:
            return None
        head = tokens[0]

        # Direct synonym lookup (exact alias match first).
        if head in SYNONYMS:
            return SYNONYMS[head] + tokens[1:]

        # Exact command match (e.g. "look at sword" stays as-is).
        if head in COMMANDS:
            return tokens

        # Bare direction ("n", "north") is shorthand for "go <dir>".
        if head in DIRECTIONS:
            return ["go", head]

        # Unique prefix match against canonical commands + synonyms.
        matches = [c for c in COMMANDS if c.startswith(head)]
        matches += [s for s in SYNONYMS if s.startswith(head)]
        if len(matches) == 1 and matches[0] in SYNONYMS:
            return SYNONYMS[matches[0]] + tokens[1:]
        if len(matches) == 1:
            return [matches[0]] + tokens[1:]

        return None

    def _resolve_direction(self, token: str) -> Optional[str]:
        """Map a direction token (full or abbreviated) to its canonical form."""
        return DIRECTIONS.get(token)

    def _find_visible_item(self, query: str) -> Optional[Item]:
        """Find an item in the current room matching the query.

        Matching is exact, then unique-prefix, then substring.
        """
        room = self.rooms[self.state.current_room]
        candidates = [self.items[i] for i in room.items]
        return self._match_item(candidates, query)

    def _find_inventory_item(self, query: str) -> Optional[Item]:
        """Find an item in the player's inventory matching the query."""
        candidates = [self.items[i] for i in self.state.inventory]
        return self._match_item(candidates, query)

    @staticmethod
    def _match_item(candidates: List[Item], query: str) -> Optional[Item]:
        """Match a query against item candidates.

        Tries, in order: exact display-name/id match, unique prefix
        match, then unique substring match.  Returns ``None`` when the
        query is ambiguous or finds nothing.
        """
        if not query:
            return None
        for item in candidates:
            if query in (item.name, item.display_name, item.display_name[1:]):
                return item
        prefix = [i for i in candidates
                  if i.name.startswith(query)
                  or i.display_name.startswith(query)]
        if len(prefix) == 1:
            return prefix[0]
        substring = [i for i in candidates
                     if query in i.name or query in i.display_name]
        if len(substring) == 1:
            return substring[0]
        return None

    # -- commands ----------------------------------------------------------------

    def cmd_look(self, args: List[str]) -> None:
        """Print the current room (or the description of a specific item)."""
        if args and args[0] == "at":
            args = args[1:]
        if args:
            query = " ".join(args)
            item = self._find_inventory_item(query) or self._find_visible_item(query)
            if item is None:
                self._emit(f"You don't see anything called '{query}' here.")
                return
            self._emit(item.description)
            return

        room = self.rooms[self.state.current_room]
        self._emit(f"{room.display_name}\n{room.description}")
        if room.items:
            names = ", ".join(self.items[i].display_name for i in room.items)
            self._emit(f"You see: {names}.")
        else:
            self._emit("There are no items of note here.")

    def cmd_go(self, args: List[str]) -> None:
        """Move the player in a given direction, respecting locked doors."""
        if not args:
            self._emit("Go where? Try 'go north'.")
            return
        direction = self._resolve_direction(args[0])
        if direction is None:
            self._emit(f"'{args[0]}' is not a direction. Try north, south, "
                       "east, or west.")
            return

        room = self.rooms[self.state.current_room]
        if direction not in room.exits:
            self._emit(f"You cannot go {direction} from here.")
            return

        # Check for a locked door on this exit.  Once a door has been
        # opened it stays open for the rest of the game.
        lock = room.locked_exits.get(direction)
        if lock is not None:
            door_flag = f"door_open_{room.name}_{direction}"
            if not self.state.has_flag(door_flag):
                if self._lock_satisfied(lock):
                    # The player has what the door needs (e.g. the key is
                    # auto-used from their inventory) -- open it for good.
                    self.state.set_flag(door_flag)
                    self._emit(lock.unlock_message)
                    self.state.record(
                        f"Unlocked the {room.display_name}'s {direction} door."
                    )
                else:
                    self._emit(lock.locked_message)
                    return

        self.state.current_room = room.exits[direction]
        self.state.visited.append(self.state.current_room)
        self.state.record(f"Went {direction} to "
                          f"{self.rooms[self.state.current_room].display_name}.")
        self.cmd_look([])

    def _lock_satisfied(self, lock: DoorLock) -> bool:
        """Return ``True`` if the player can currently pass the lock."""
        if lock.required_item is not None and lock.required_item not in self.state.inventory:
            return False
        if lock.required_flag is not None and not self.state.has_flag(lock.required_flag):
            return False
        return True

    def cmd_take(self, args: List[str]) -> None:
        """Pick up an item from the current room into the inventory."""
        query = " ".join(args)
        if not query:
            self._emit("Take what?")
            return
        item = self._find_visible_item(query)
        if item is None:
            self._emit(f"There is no '{query}' here to take.")
            return
        room = self.rooms[self.state.current_room]
        room.items.remove(item.name)
        self.state.inventory.append(item.name)
        self._emit(f"You pick up {item.display_name}.")
        self.state.record(f"Took {item.display_name}.")

        # Side effects: carrying the torch lights the Armory.
        if item.name == "torch" and self.state.current_room == "armory":
            self.rooms["armory"].description = (
                "Racks of rusted swords and dented shields line the walls. "
                "The flickering light of your torch dances across the steel, "
                "and a heavy iron door bars the way north."
            )
            self._emit("The flickering light of your torch spills across "
                       "the armory's dusty racks.")

    def cmd_drop(self, args: List[str]) -> None:
        """Drop an item from the inventory into the current room."""
        query = " ".join(args)
        if not query:
            self._emit("Drop what?")
            return
        item = self._find_inventory_item(query)
        if item is None:
            self._emit(f"You are not carrying '{query}'.")
            return
        self.state.inventory.remove(item.name)
        self.rooms[self.state.current_room].items.append(item.name)
        self._emit(f"You drop {item.display_name}.")
        self.state.record(f"Dropped {item.display_name}.")

    def cmd_inventory(self, args: List[str]) -> None:
        """List the player's inventory."""
        if not self.state.inventory:
            self._emit("Your inventory is empty.")
            return
        listing = ", ".join(self.items[i].display_name
                            for i in self.state.inventory)
        self._emit(f"You are carrying: {listing}.")

    def cmd_use(self, args: List[str]) -> None:
        """Use an item from the inventory, triggering its on_use callback."""
        query = " ".join(args)
        if not query:
            self._emit("Use what?")
            return
        # Items can be used from the inventory, or in place if they are
        # simply within reach in the current room (e.g. the library lever).
        item = self._find_inventory_item(query) or self._find_visible_item(query)
        if item is None:
            self._emit(f"You have no '{query}' to use.")
            return
        if item.on_use is None:
            self._emit(f"You find {item.display_name} to be purely "
                       "decorative. It does nothing useful.")
            return
        message = item.on_use(self.state)
        if message:
            self._emit(message)
            self.state.record(f"Used {item.display_name}.")

    def cmd_help(self, args: List[str]) -> None:
        """Print a list of all available commands."""
        self._emit(
            "Available commands:\n"
            "  look [at <item>]   Look around, or examine an item.\n"
            "  go <direction>     Move (n/s/e/w work too; bare 'n' works).\n"
            "  move <direction>   Same as go.\n"
            "  take <item>        Pick up an item (synonyms: grab, get).\n"
            "  drop <item>        Leave an item in the current room.\n"
            "  use <item>         Activate an item's special effect.\n"
            "  inventory          List what you are carrying (i, inv).\n"
            "  history            Show the last 10 events.\n"
            "  help               Show this help.\n"
            "  quit               Leave the dungeon.\n"
            "Synonyms: 'examine' = 'look at', 'grab'/'get' = 'take'."
        )

    def cmd_history(self, args: List[str]) -> None:
        """Print the last 10 recorded events."""
        recent = self.state.history[-10:]
        if not recent:
            self._emit("Nothing has happened yet.")
            return
        self._emit("Last 10 events:")
        for i, event in enumerate(recent, start=1):
            self._emit(f"  {i}. {event}")

    def cmd_quit(self, args: List[str]) -> None:
        """Print a farewell message.  The game loop detects and exits."""
        rooms = len(self.state.visited)
        self._emit(
            f"Goodbye, adventurer! You explored {rooms} "
            f"{'room' if rooms == 1 else 'rooms'} "
            f"and found the secret "
            f"{'before the dungeon found you.' if self.state.has_flag('won') else 'somewhere else, it seems.'}"
        )

    # -- dispatch -----------------------------------------------------------------

    def handle_input(self, raw: str) -> bool:
        """Parse and execute one line of player input.

        Returns:
            ``False`` if the player quit and the game loop should stop,
            otherwise ``True``.
        """
        tokens = self._parse_command(raw)
        self.print_separator()
        if not tokens:
            return True

        resolved = self._resolve_command(tokens)
        if resolved is None:
            self._emit(f"I don't understand '{tokens[0]}'. Type 'help' for "
                       "a list of commands.")
            return True

        head = resolved[0]
        args = resolved[1:]

        if head == "quit":
            self.cmd_quit(args)
            return False
        if head == "look":
            self.cmd_look(args)
        elif head in ("go", "move"):
            self.cmd_go(args)
        elif head == "take":
            self.cmd_take(args)
        elif head == "drop":
            self.cmd_drop(args)
        elif head == "use":
            self.cmd_use(args)
        elif head == "inventory":
            self.cmd_inventory(args)
        elif head == "help":
            self.cmd_help(args)
        elif head == "history":
            self.cmd_history(args)
        else:  # pragma: no cover - defensive; resolver only returns known heads
            self._emit("I don't understand that. Type 'help' for a list "
                       "of commands.")
        return True

    # -- output helpers --------------------------------------------------------------

    def print_separator(self) -> None:
        """Print the separator line shown after each command."""
        print("-" * 60)

    def _emit(self, message: str) -> None:
        """Print a message to the player and record it in the history."""
        print(message)
        self.state.record(message.splitlines()[0] if message else "")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Create the game, print the banner, and run the interactive loop."""
    engine = GameEngine()
    engine.banner()

    while True:
        try:
            raw = input("\n> ")
        except (EOFError, KeyboardInterrupt):
            print()
            print("The dungeon fades to darkness. Goodbye, adventurer!")
            break
        if not engine.handle_input(raw):
            print("The dungeon fades to darkness. Goodbye, adventurer!")
            break


if __name__ == "__main__":
    main()
