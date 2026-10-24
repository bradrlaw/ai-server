"""A text-based adventure game engine.

Implements a small, self-contained interactive fiction engine using only the
Python standard library. The engine is built around three core classes:

- :class:`GameState`  -- mutable game state (room, inventory, flags, history)
- :class:`Room`       -- a location with exits, items and optional locked doors
- :class:`Item`       -- a collectable object with an optional ``on_use`` hook

:class:`GameEngine` wires everything together: it parses player input, routes
commands, and renders output. A sample dungeon ("A Small Dungeon") is built by
:func:`build_dungeon` and launched from the ``__main__`` block.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEPARATOR = "-" * 60
DIRECTIONS = ("north", "south", "east", "west")

# Direction aliases accepted from the player, mapped to canonical directions.
DIRECTION_ALIASES: Dict[str, str] = {
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
    "up": "north",   # generous fallbacks for simple layouts
    "down": "south",
}

# Maps a raw command word to its canonical form.
COMMAND_SYNONYMS: Dict[str, str] = {
    "examine": "look",
    "inspect": "look",
    "grab": "take",
    "get": "take",
    "pick": "take",
    "i": "inventory",
    "inv": "inventory",
    "move": "go",
    "walk": "go",
    "q": "quit",
    "exit": "quit",
    "e": "east",
    "w": "west",
    "n": "north",
    "s": "south",
}


# ---------------------------------------------------------------------------
# Core data model
# ---------------------------------------------------------------------------


@dataclass
class Item:
    """A collectable object that can exist in a room or in inventory.

    Attributes:
        name: Lowercase identifier used for matching player input.
        description: Flavor text shown when the item is examined.
        on_use: Optional callback invoked by the ``use`` command. It receives
            the active :class:`GameState` and returns a message to display
            (or ``None`` for no extra output).
    """

    name: str
    description: str
    on_use: Optional[Callable[["GameState"], Optional[str]]] = None

    def use(self, state: "GameState") -> Optional[str]:
        """Activate the item's effect and return any output message."""
        if self.on_use is None:
            return f"Nothing obvious happens when you use the {self.name}."
        return self.on_use(state)


@dataclass
class Room:
    """A location in the dungeon.

    Attributes:
        id: Canonical identifier used by exits and the engine.
        name: Display name shown in room headers.
        description: Base room description.
        exits: Mapping of canonical direction -> target room id.
        items: Item ids currently lying on the floor of this room.
        locked_exits: Directions that are blocked; each entry records the
            display label of the obstruction and the item name that opens it
            (``None`` means the lock is opened by a world event, not an item).
        dynamic_description: Optional callable returning an alternate
            description when a world flag is set, e.g. after a lever is pulled.
    """

    id: str
    name: str
    description: str
    exits: Dict[str, str] = field(default_factory=dict)
    items: List[str] = field(default_factory=list)
    locked_exits: Dict[str, tuple] = field(default_factory=dict)
    dynamic_description: Optional[Callable[["GameState"], Optional[str]]] = None

    def describe(self, state: "GameState") -> str:
        """Return the room description appropriate for the current state."""
        if self.dynamic_description is not None:
            alt = self.dynamic_description(state)
            if alt is not None:
                return alt
        return self.description


@dataclass
class GameState:
    """Everything that can change while the game is being played.

    Attributes:
        current_room: Id of the room the player occupies.
        inventory: Names of items the player is carrying.
        visited: Ids of rooms the player has entered at least once.
        flags: Boolean world-state flags (e.g. ``lever_pulled``).
        history: Log of every message shown to the player this session.
        rooms: Registry of all rooms, keyed by room id.
        items: Registry of all items, keyed by item name.
        won: Set to ``True`` when the secret ending is triggered.
    """

    current_room: str = ""
    inventory: List[str] = field(default_factory=list)
    visited: List[str] = field(default_factory=list)
    flags: Dict[str, bool] = field(default_factory=dict)
    history: List[str] = field(default_factory=list)
    rooms: Dict[str, Room] = field(default_factory=dict)
    items: Dict[str, Item] = field(default_factory=dict)
    won: bool = False

    # -- helpers ------------------------------------------------------------

    def set_flag(self, name: str, value: bool = True) -> None:
        """Set a world-state flag."""
        self.flags[name] = value

    def has_flag(self, name: str) -> bool:
        """Return ``True`` if the named flag is set."""
        return bool(self.flags.get(name, False))

    def log(self, message: str) -> None:
        """Append a message to the event history."""
        self.history.append(message)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class GameEngine:
    """Parses player input, routes commands, and renders output.

    The engine holds no mutable state of its own beyond a ``quit`` marker;
    all world/player state lives inside the supplied :class:`GameState`.
    """

    def __init__(self, state: GameState, out: Callable[[str], None] = print) -> None:
        """Initialize the engine.

        Args:
            state: The fully-built game state to operate on.
            out: Output sink; defaults to :func:`print`. Injectable for tests.
        """
        self.state = state
        self._out = out
        self._quit = False

    # -- public API ----------------------------------------------------------

    def run(self) -> None:
        """Main loop: banner, opening room, then read-eval-print until quit."""
        self._print_banner()
        self.state.log("Game started.")
        self._handle_look([])
        while not self._quit:
            try:
                raw = input("> ").strip()
            except EOFError:
                self._goodbye("The story fades as you leave the dungeon.")
                break
            except KeyboardInterrupt:
                self._goodbye("Until next time, adventurer.")
                break
            if not raw:
                continue
            self.dispatch(raw)
            self._out(SEPARATOR)

    def dispatch(self, raw: str) -> None:
        """Parse a single line of player input and execute it.

        Args:
            raw: The raw command line typed by the player.
        """
        try:
            self._execute(raw)
        except EOFError:
            self._goodbye("The story fades as you leave the dungeon.")
        except KeyboardInterrupt:
            self._goodbye("Until next time, adventurer.")

    # -- command routing ------------------------------------------------------

    def _execute(self, raw: str) -> None:
        """Resolve and run one command; every command prints a separator."""
        self._out(SEPARATOR)
        parts = raw.lower().split()
        verb, args = parts[0], parts[1:]

        # A bare direction (or alias like "n") acts as "go <direction>".
        canonical_verb = COMMAND_SYNONYMS.get(verb, verb)
        if canonical_verb in DIRECTIONS:
            self._handle_go([canonical_verb])
            self.state.log(f"> {raw}")
            return

        handler: Optional[Callable[[List[str]], None]] = {
            "look": self._handle_look,
            "go": self._handle_go,
            "take": self._handle_take,
            "drop": self._handle_drop,
            "inventory": self._handle_inventory,
            "use": self._handle_use,
            "help": self._handle_help,
            "history": self._handle_history,
            "quit": self._handle_quit,
        }.get(self._match_command(canonical_verb))

        if handler is None:
            message = (
                f"I don't understand '{raw}'. Type 'help' for a list of commands."
            )
            self._out(message)
            self.state.log(message)
            return

        handler(args)
        self.state.log(f"> {raw}")

    def _match_command(self, verb: str) -> Optional[str]:
        """Return the unique canonical command starting with ``verb``.

        Implements partial matching: "inv" -> "inventory", "his" -> "history".
        Returns ``None`` when nothing or multiple commands match ambiguously
        (ambiguity is resolved in favor of exact/known synonyms first).
        """
        known = (
            "look",
            "go",
            "take",
            "drop",
            "inventory",
            "use",
            "help",
            "history",
            "quit",
        )
        if verb in known:
            return verb
        matches = [cmd for cmd in known if cmd.startswith(verb)]
        if len(matches) == 1:
            return matches[0]
        return None

    # -- individual commands ---------------------------------------------------

    def _handle_look(self, args: List[str]) -> None:
        """Handle ``look`` and ``look at <thing>``."""
        if not args:
            self._describe_room()
            return
        if args[0] == "at":
            args = args[1:]
        target = " ".join(args)
        self._describe_thing(target)

    def _describe_room(self) -> None:
        """Print the current room header, description, exits and items."""
        room = self.state.rooms[self.state.current_room]
        visible = [self.state.items[i].name for i in room.items]
        lines = [
            f"== {room.name} ==",
            room.describe(self.state),
        ]
        exits = sorted(room.exits)
        self._out("\n".join(lines))
        self._out(f"Exits: {', '.join(exits) if exits else 'none'}.")
        self._out(
            "You see: " + (", ".join(visible) if visible else "nothing of interest.")
        )
        self.state.log(f"Looked around {room.name}.")

    def _describe_thing(self, target: str) -> None:
        """Print the description of an item, preferring inventory then room."""
        item = self._find_item(target, include_inventory=True, include_room=True)
        if item is not None:
            self._out(f"{item.name}: {item.description}")
            self.state.log(f"Examined the {item.name}.")
            return
        if "inscription" in target or "riddle" in target:
            text = (
                "The inscription reads: 'Peace is the true treasure; "
                "the scroll shows the way.'"
            )
            self._out(text)
            self.state.log("Read the riddle inscription.")
            return
        message = f"You don't see any '{target}' here."
        self._out(message)
        self.state.log(message)

    def _handle_go(self, args: List[str]) -> None:
        """Handle ``go <direction>`` including locked-door checks."""
        if not args:
            message = "Go where? Specify a direction (north, south, east, west)."
            self._out(message)
            self.state.log(message)
            return
        direction = DIRECTION_ALIASES.get(args[0], args[0])
        if direction not in DIRECTIONS:
            message = f"'{args[0]}' is not a direction. Try north/south/east/west."
            self._out(message)
            self.state.log(message)
            return

        room = self.state.rooms[self.state.current_room]
        if direction not in room.exits:
            message = f"You can't go {direction} from here."
            self._out(message)
            self.state.log(message)
            return

        # Locked doors may require an item (auto-used) or a world event.
        if direction in room.locked_exits:
            label, required_item = room.locked_exits[direction]
            if required_item is not None:
                if required_item in self.state.inventory:
                    # Auto-use the key: unlock permanently and pass through.
                    room.locked_exits.pop(direction)
                    self.state.set_flag(f"unlocked_{room.id}_{direction}")
                    self._out(
                        f"You unlock the {label} with the {required_item}. "
                        "It swings open."
                    )
                    self.state.log(
                        f"Unlocked the {label} in {room.name} using the {required_item}."
                    )
                else:
                    message = (
                        f"The way {direction} is blocked by {label}. "
                        f"You need the {required_item} to open it."
                    )
                    self._out(message)
                    self.state.log(message)
                    return
            else:
                message = f"The way {direction} is blocked by {label}."
                self._out(message)
                self.state.log(message)
                return

        self._move_to(room.exits[direction], direction)

    def _move_to(self, room_id: str, direction: str) -> None:
        """Move the player to ``room_id`` and describe the arrival."""
        self.state.current_room = room_id
        if room_id not in self.state.visited:
            self.state.visited.append(room_id)
        self.state.log(f"Went {direction} to {self.state.rooms[room_id].name}.")
        self._describe_room()
        if self.state.rooms[room_id].id == "garden":
            self._out("A message is scrawled on a garden stone: \"You've found peace.\"")
            self.state.log("Read the garden message: You've found peace.")

    def _handle_take(self, args: List[str]) -> None:
        """Handle ``take <item>``."""
        if not args:
            message = "Take what?"
            self._out(message)
            self.state.log(message)
            return
        target = " ".join(args)
        item = self._find_item(target, include_inventory=False, include_room=True)
        if item is None:
            message = f"There is no '{target}' here to take."
            self._out(message)
            self.state.log(message)
            return
        room = self.state.rooms[self.state.current_room]
        room.items.remove(item.name)
        self.state.inventory.append(item.name)
        self._out(f"You take the {item.name}.")
        self.state.log(f"Took the {item.name}.")
        if item.name == "torch":
            self.state.set_flag("torch_taken")

    def _handle_drop(self, args: List[str]) -> None:
        """Handle ``drop <item>``."""
        if not args:
            message = "Drop what?"
            self._out(message)
            self.state.log(message)
            return
        target = " ".join(args)
        item = self._find_item(target, include_inventory=True, include_room=False)
        if item is None:
            message = f"You're not carrying any '{target}'."
            self._out(message)
            self.state.log(message)
            return
        self.state.inventory.remove(item.name)
        self.state.rooms[self.state.current_room].items.append(item.name)
        self._out(f"You drop the {item.name}.")
        self.state.log(f"Dropped the {item.name}.")
        if item.name == "torch":
            self.state.set_flag("torch_taken", False)

    def _handle_inventory(self, args: List[str]) -> None:
        """Handle ``inventory`` / ``i``."""
        if self.state.inventory:
            self._out("You are carrying: " + ", ".join(sorted(self.state.inventory)) + ".")
            self.state.log("Checked inventory.")
        else:
            self._out("Your hands are empty.")
            self.state.log("Checked inventory (empty).")

    def _handle_use(self, args: List[str]) -> None:
        """Handle ``use <item>`` by invoking the item's ``on_use`` callback."""
        if not args:
            message = "Use what?"
            self._out(message)
            self.state.log(message)
            return
        target = " ".join(args)
        item = self._find_item(target, include_inventory=True, include_room=True)
        if item is None:
            message = f"You don't have or see any '{target}'."
            self._out(message)
            self.state.log(message)
            return
        result = item.use(self.state)
        if result:
            self._out(result)
            self.state.log(result)
        if self.state.won:
            victory = "*** VICTORY! You uncovered the secret ending! ***"
            self._out(victory)
            self.state.log(victory)

    def _handle_help(self, args: List[str]) -> None:
        """Handle ``help`` -- print the command reference."""
        self._out(
            "\n".join(
                [
                    "Available commands:",
                    "  look                 Describe the current room",
                    "  look at <item>       Examine an item (or 'examine <item>')",
                    "  go <direction>       Move (n/s/e/w also work)",
                    "  take <item>          Pick up an item (or 'grab')",
                    "  drop <item>          Drop a carried item",
                    "  inventory / i        List carried items",
                    "  use <item>           Activate an item's special effect",
                    "  history              Show the last 10 events",
                    "  help                 This message",
                    "  quit                 Leave the dungeon",
                ]
            )
        )
        self.state.log("Asked for help.")

    def _handle_history(self, args: List[str]) -> None:
        """Handle ``history`` -- show the last 10 logged events."""
        recent = self.state.history[-10:]
        if not recent:
            self._out("No events yet.")
            return
        self._out("Recent events:")
        for i, event in enumerate(recent, start=1):
            self._out(f"  {i}. {event}")
        self.state.log("Reviewed history.")

    def _handle_quit(self, args: List[str]) -> None:
        """Handle ``quit`` -- end the loop with a farewell."""
        self._goodbye("Thanks for playing! The dungeon dims behind you.")
        self._quit = True

    # -- item / input helpers ---------------------------------------------------

    def _find_item(
        self,
        target: str,
        include_inventory: bool,
        include_room: bool,
    ) -> Optional[Item]:
        """Find an item by partial-name match in inventory and/or the room.

        Args:
            target: Player-supplied item text (lowercase).
            include_inventory: Search the player's inventory first.
            include_room: Search the current room's floor items.

        Returns:
            The first matching :class:`Item`, or ``None``.
        """
        target = target.replace("the ", "").replace("a ", "").strip()
        candidate_names: List[str] = []
        if include_inventory:
            candidate_names.extend(self.state.inventory)
        if include_room:
            candidate_names.extend(self.state.rooms[self.state.current_room].items)
        for name in candidate_names:
            # Match whole word or unique prefix: "key" matches "rusted key",
            # "scr" matches "ancient scroll".
            words = name.split()
            if target == name or target in words or name.startswith(target) or any(
                w.startswith(target) for w in words
            ):
                return self.state.items[name]
        return None

    # -- presentation -------------------------------------------------------------

    def _print_banner(self) -> None:
        """Print the styled ASCII opening banner."""
        self._out(
            "\n".join(
                [
                    "",
                    r"  ____    _    ____  _   _  ____ _____  _    _   _    _    _   _ ",
                    r" / ___|  / \  |  _ \| | | |/ ___|_   _|/ \  | \ | |  / \  | \ | |",
                    r" \___ \ / _ \ | |_) | |_| | |     | | / _ \ |  \| | / _ \ |  \| |",
                    r"  ___) / ___ \|  __/|  _  | |___  | |/ ___ \| |\  |/ ___ \| |\  |",
                    r" |____/_/   \_\_|   |_| |_| \____| |_/_/   \_\_| \_/_/   \_\_| \_|",
                    "",
                    "        A  T E X T - B A S E D  A D V E N T U R E",
                    "",
                    "Type 'help' for commands, 'quit' to leave.",
                    "",
                ]
            )
        )

    def _goodbye(self, message: str) -> None:
        """Print a farewell message and request loop termination."""
        self._out(SEPARATOR)
        self._out(message)
        self.state.log(message)
        self._quit = True


# ---------------------------------------------------------------------------
# The dungeon world
# ---------------------------------------------------------------------------


def pull_lever(state: GameState) -> Optional[str]:
    """``on_use`` effect for the Library lever.

    Permanently opens the secret passage west out of the Library and removes
    the bookshelf obstruction.
    """
    if state.has_flag("lever_pulled"):
        return "The lever is already in its new position; nothing more happens."
    state.set_flag("lever_pulled")
    library = state.rooms["library"]
    library.locked_exits.pop("west", None)
    library.exits.setdefault("west", "secret_chamber")
    return (
        "With a grinding roar, the dusty bookshelf slides aside, "
        "revealing a dark secret passage heading west!"
    )


def read_scroll(state: GameState) -> Optional[str]:
    """``on_use`` effect for the ancient scroll: reveals the secret ending."""
    if state.won:
        return "The scroll's words have already reshaped your fate."
    state.won = True
    return (
        "You unroll the ancient scroll. Its ink shimmers and rewrites itself "
        "into your own hand:\n\n"
        "    'You who read these words have completed the True Quest. The "
        "dungeon was a mirror; the treasure was persistence.'\n\n"
        "Warm light fills the chamber as the secret ending unfolds."
    )


def use_torch(state: GameState) -> Optional[str]:
    """``on_use`` effect for the torch when not in a dark place."""
    if state.current_room == "armory":
        return (
            "You raise the torch; shadows recoil and hidden weapon racks "
            "glint in the flickering light."
        )
    return "You wave the torch around. It burns steadily, illuminating nothing new."


def use_key(state: GameState) -> Optional[str]:
    """``on_use`` effect for the rusted key outside the Armory."""
    if state.current_room == "armory":
        return (
            "The rusted key looks like it belongs in the door to the north, "
            "but it only works when you try that door."
        )
    return "There is no keyhole here for the rusted key."


def coin_description(state: GameState) -> str:
    """Return the witty golden coin description."""
    return (
        "A gleaming golden coin. On one side, a king's profile; on the other, "
        "a fine print reading 'Good for one (1) round trip to Regret, "
        "non-transferable, no refunds.' It is worth a great deal of bragging "
        "rights and precisely nothing else."
    )


def library_description(state: GameState) -> Optional[str]:
    """Alternate Library description shown after the lever is pulled."""
    if state.has_flag("lever_pulled"):
        return (
            "Towering shelves sag under the weight of forgotten centuries. "
            "Where the dusty bookshelf once blocked the way, an open archway "
            "now breathes cold air from a secret passage to the west."
        )
    return None


def armory_description(state: GameState) -> Optional[str]:
    """Alternate Armory description when the player carries the lit torch."""
    if state.has_flag("torch_taken") and "torch" in state.inventory:
        return (
            "Rusted weapon racks line the walls, and a locked door of black "
            "iron stands to the north. Your torch casts a flickering light "
            "over dented shields and a long-abandoned anvil."
        )
    return None


def build_dungeon() -> GameEngine:
    """Construct the sample dungeon and return a ready-to-run engine.

    Layout::

                      Treasure Room
                           |  (locked: rusted key)
        Secret Chamber -- Library --  --  --  -- Armory
             (lever)         |                    |
                        Entrance Hall ------ (torch inside)
                             |
                          Garden

    Returns:
        A :class:`GameEngine` whose state starts in the Entrance Hall.
    """
    state = GameState()

    # -- items ----------------------------------------------------------------
    items = {
        "rusted key": Item(
            name="rusted key",
            description=(
                "A squat iron key eaten orange by rust. It still looks "
                "stubbornly determined to open something."
            ),
            on_use=use_key,
        ),
        "lever": Item(
            name="lever",
            description=(
                "A brass lever protruding from the wall behind the shelves, "
                "green with age. It begs to be pulled."
            ),
            on_use=pull_lever,
        ),
        "torch": Item(
            name="torch",
            description="A resinous torch, dry and ready to burn.",
            on_use=use_torch,
        ),
        "golden coin": Item(
            name="golden coin",
            description=coin_description(state),
        ),
        "ancient scroll": Item(
            name="ancient scroll",
            description=(
                "A brittle scroll sealed with black wax. Reading the "
                "inscription seems wise... or regrettable."
            ),
            on_use=read_scroll,
        ),
    }
    state.items = items

    # -- rooms ------------------------------------------------------------------
    rooms = {
        "entrance": Room(
            id="entrance",
            name="Entrance Hall",
            description=(
                "A drafty stone hall where your adventure begins. "
                "Torch brackets claw at empty air, and dust motes drift in "
                "the pale light from a cracked doorway."
            ),
            exits={"north": "library", "east": "armory", "south": "garden"},
            items=["rusted key"],
        ),
        "library": Room(
            id="library",
            name="Library",
            description=(
                "Shelves tower into darkness, groaning under forgotten tomes. "
                "A massive dusty bookshelf has slid sideways, completely "
                "blocking the way back south... wait, no - it blocks a "
                "section of wall to the west, and a brass lever peeks out "
                "from behind the spines."
            ),
            exits={"south": "entrance", "west": "secret_chamber"},
            items=["lever"],
            locked_exits={"west": ("a dusty bookshelf", None)},
            dynamic_description=library_description,
        ),
        "armory": Room(
            id="armory",
            name="Armory",
            description=(
                "Rusted weapon racks line the walls beneath a locked door of "
                "black iron to the north. Something metallic glitters on the "
                "floor among the debris."
            ),
            exits={"west": "entrance", "north": "treasure"},
            items=["torch"],
            locked_exits={"north": ("a locked iron door", "rusted key")},
            dynamic_description=armory_description,
        ),
        "treasure": Room(
            id="treasure",
            name="Treasure Room",
            description=(
                "A vaulted chamber. Most shelves stand empty and pickaxed, "
                "but a single golden coin remains, and a riddle is carved "
                "into the far wall ('look at inscription')."
            ),
            exits={"south": "armory"},
            items=["golden coin"],
        ),
        "secret_chamber": Room(
            id="secret_chamber",
            name="Secret Chamber",
            description=(
                "A hidden room untouched by time. Moonlight falls through a "
                "crack in the ceiling onto a pedestal, where an ancient "
                "scroll waits for its reader."
            ),
            exits={"east": "library"},
            items=["ancient scroll"],
        ),
        "garden": Room(
            id="garden",
            name="Garden",
            description=(
                "A peaceful clearing open to the sky. Birds sing; a brook "
                "murmurs over smooth stones. A message is scratched into a "
                "flat garden stone."
            ),
            exits={"north": "entrance"},
            items=[],
        ),
    }
    state.rooms = rooms
    state.current_room = "entrance"
    state.visited.append("entrance")
    return GameEngine(state)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        build_dungeon().run()
    except (EOFError, KeyboardInterrupt):
        print("\nGoodbye, adventurer. The dungeon will remember you.")
    sys.exit(0)
