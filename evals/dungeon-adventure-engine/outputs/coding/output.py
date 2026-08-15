"""
Dungeon of Echoes — A Text-Based Adventure Game Engine
=======================================================

A complete, self-contained text adventure built with Python's standard library only.
Features a mini dungeon with 6 rooms, 5 interactive items, locked doors,
dynamic room descriptions, and a secret ending.

Run directly: python dungeon.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable, Optional


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Item:
    """Represents a tangible object the player can interact with.

    Attributes:
        item_id: Unique identifier used in room/excit references.
        name: Display name shown to the player.
        description: Text shown when the player examines the item.
        on_use: Optional callback invoked when the player uses the item.
                Receives the current GameState and returns a message string.
    """

    item_id: str
    name: str
    description: str
    on_use: Optional[Callable[["GameState"], str]] = None


@dataclass
class LockInfo:
    """Describes a locked door and what is needed to open it.

    Attributes:
        requires: Either an item_id (player must hold it) or a flag name
                  prefixed with "flag:" (the flag must be True).
        locked_message: Shown when the player attempts to pass while locked.
        unlock_message: Shown when the lock is satisfied.
    """

    requires: str
    locked_message: str
    unlock_message: str = "The lock clicks open."


@dataclass
class Room:
    """Represents a single location in the game world.

    Attributes:
        room_id: Unique identifier for the room.
        name: Display name.
        description: Base description text.
        dynamic_description: Optional callback that returns a modified
                description based on current game state (e.g., items present,
                flags set). If None, `description` is used as-is.
        exits: Mapping of direction ("north", "south", etc.) to target room_id.
        items: List of item_ids currently present in this room.
        locked_doors: Mapping of direction to LockInfo for blocked passages.
    """

    room_id: str
    name: str
    description: str
    exits: dict[str, str] = field(default_factory=dict)
    items: list[str] = field(default_factory=list)
    locked_doors: dict[str, LockInfo] = field(default_factory=dict)
    dynamic_description: Optional[Callable[["GameState"], str]] = None


# ─────────────────────────────────────────────────────────────────────────────
# GAME STATE
# ─────────────────────────────────────────────────────────────────────────────


class GameState:
    """Encapsulates all mutable game state in a single object.

    This class holds no logic beyond storage; all mutation happens through
    the GameEngine to maintain a single point of control.

    Attributes:
        current_room: The room_id the player is currently in.
        inventory: Ordered list of item_ids carried by the player.
        visited_rooms: Set of room_ids the player has entered at least once.
        flags: Dictionary of boolean game-state flags (e.g., "lever_pulled").
        history: Chronological list of all event/message strings.
    """

    def __init__(self, starting_room: str) -> None:
        """Initialize game state at the given starting room."""
        self.current_room: str = starting_room
        self.inventory: list[str] = []
        self.visited_rooms: set[str] = {starting_room}
        self.flags: dict[str, bool] = {}
        self.history: list[str] = []

    def record(self, message: str) -> None:
        """Append a message to the game history log."""
        self.history.append(message)


# ─────────────────────────────────────────────────────────────────────────────
# GAME ENGINE
# ─────────────────────────────────────────────────────────────────────────────


class GameEngine:
    """Core game engine: world definition, input parsing, command execution,
    and output rendering.

    The engine owns the Room/Item registry and the GameState, and exposes a
    single `run()` method that starts the interactive game loop.
    """

    # ── Command and direction aliases (partial-match enabled) ──

    _COMMANDS: dict[str, str] = {
        "look": "look",
        "examine": "look_at",
        "exam": "look_at",
        "go": "go",
        "move": "go",
        "walk": "go",
        "take": "take",
        "grab": "take",
        "get": "take",
        "drop": "drop",
        "put": "drop",
        "inventory": "inventory",
        "i": "inventory",
        "inv": "inventory",
        "inven": "inventory",
        "use": "use",
        "activate": "use",
        "help": "help",
        "h": "help",
        "history": "history",
        "hist": "history",
        "quit": "quit",
        "q": "quit",
        "exit": "quit",
    }

    _DIRECTIONS: dict[str, str] = {
        "north": "north",
        "n": "north",
        "no": "north",
        "nor": "north",
        "nort": "north",
        "south": "south",
        "s": "south",
        "so": "south",
        "sou": "south",
        "souh": "south",
        "east": "east",
        "e": "east",
        "ea": "east",
        "west": "west",
        "w": "west",
        "we": "west",
    }

    def __init__(self) -> None:
        """Build the dungeon world and initialize the game state."""
        self.items: dict[str, Item] = {}
        self.rooms: dict[str, Room] = {}
        self.state: GameState = GameState(starting_room="entrance")
        self._build_world()

    # ─────────────────────────────────────────────────────────────────────
    # WORLD CONSTRUCTION
    # ─────────────────────────────────────────────────────────────────────

    def _build_world(self) -> None:
        """Construct all rooms and items for the mini-dungeon."""

        # ── Items ──

        self.items["rusted_key"] = Item(
            item_id="rusted_key",
            name="Rusted Key",
            description=(
                "A small iron key, green with rust. It looks old but "
                "structurally sound. Perhaps it opens something nearby."
            ),
        )

        self.items["lever"] = Item(
            item_id="lever",
            name="Iron Lever",
            description=(
                "A heavy iron lever set into the bookshelf's base. It "
                "feels like it's meant to be pulled, not admired."
            ),
            on_use=self._use_lever,
        )

        self.items["torch"] = Item(
            item_id="torch",
            name="Torch",
            description=(
                "A sturdy oak torch with a fresh wick. It hasn't been "
                "lit yet, but it smells of tallow and promise."
            ),
        )

        self.items["golden_coin"] = Item(
            item_id="golden_coin",
            name="Golden Coin",
            description=(
                "A gleaming gold coin bearing the face of a king who "
                "definitely wasn't on that many other coins. You could "
                "buy a sandwich with this. Or a very fancy one."
            ),
        )

        self.items["ancient_scroll"] = Item(
            item_id="ancient_scroll",
            name="Ancient Scroll",
            description=(
                "A brittle scroll sealed with faded wax. Faint luminescent "
                "ink glows between the folds. The text seems to shift "
                "when you're not looking directly at it."
            ),
            on_use=self._use_scroll,
        )

        # ── Rooms ──

        self.rooms["entrance"] = Room(
            room_id="entrance",
            name="Entrance Hall",
            description=(
                "You stand in a dim stone hall. Moss creeps up the walls "
                "and water drips somewhere in the dark. A rusted key lies "
                "half-buried in the dirt on the floor."
            ),
            exits={"north": "library", "east": "armory", "south": "garden"},
            items=["rusted_key"],
        )

        self.rooms["library"] = Room(
            room_id="library",
            name="Library",
            description=(
                "Towering bookshelves line every wall, their spines cracked "
                "and titles illegible. The air smells of decay and old "
                "parchment. A heavy iron lever is embedded in the base of "
                "the northern bookshelf."
            ),
            exits={"south": "entrance", "west": "secret_chamber"},
            items=["lever"],
            locked_doors={
                "west": LockInfo(
                    requires="flag:lever_pulled",
                    locked_message=(
                        "A massive bookshelf blocks the western passage. "
                        "It's fused into the wall by some mechanism. You "
                        "notice a lever nearby that might operate it."
                    ),
                    unlock_message="The bookshelf grinds aside with a deep groan.",
                )
            },
            dynamic_description=self._library_description,
        )

        self.rooms["armory"] = Room(
            room_id="armory",
            name="Armory",
            description=(
                "Shelves of corroded weapons line the walls — swords, "
                "spear tips, dented shields. A short torch rests on a "
                "pedestal near the entrance. A heavy iron door with a "
                "keyhole stands to the north, sealed shut."
            ),
            exits={"west": "entrance", "north": "treasure"},
            items=["torch"],
            locked_doors={
                "north": LockInfo(
                    requires="rusted_key",
                    locked_message=(
                        "The iron door is locked. A small keyhole glints "
                        "in the dim light. You'll need a key to get past "
                        "this."
                    ),
                    unlock_message=(
                        "The rusted key slides into the lock with a "
                        "satisfying *clunk*. The iron door swings open."
                    ),
                )
            },
            dynamic_description=self._armory_description,
        )

        self.rooms["treasure"] = Room(
            room_id="treasure",
            name="Treasure Room",
            description=(
                "A vaulted chamber of polished obsidian. In the center, "
                "on a marble pedestal, a single golden coin catches the "
                "light from cracks in the ceiling. An inscription is "
                "carved into the floor: 'The true treasure was the "
                "journey. Or maybe it was the scroll. I forgot. — The "
                "Architect'"
            ),
            exits={"south": "armory"},
            items=["golden_coin"],
        )

        self.rooms["secret_chamber"] = Room(
            room_id="secret_chamber",
            name="Secret Chamber",
            description=(
                "A hidden room behind the library's bookshelf. The walls "
                "are lined with glowing runes that pulse in slow rhythm. "
                "On a stone altar sits an ancient scroll, faintly "
                "luminous. The air hums with quiet power."
            ),
            exits={"east": "library"},
            items=["ancient_scroll"],
        )

        self.rooms["garden"] = Room(
            room_id="garden",
            name="Garden",
            description=(
                "You step out into a peaceful clearing. Moonflowers bloom "
                "in impossible colors, and a gentle breeze carries the "
                "scent of jasmine. For a moment, the dungeon feels far "
                "away. A small stone sign reads: 'You've found peace.'"
            ),
            exits={"north": "entrance"},
            items=[],
        )

    # ─────────────────────────────────────────────────────────────────────
    # DYNAMIC DESCRIPTIONS
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _library_description(state: GameState) -> str:
        """Return the Library's description, modified by game flags."""
        base = (
            "Towering bookshelves line every wall, their spines cracked "
            "and titles illegible. The air smells of decay and old "
            "parchment."
        )
        if state.flags.get("lever_pulled", False):
            base += (
                " The northern bookshelf has been retracted into the "
                "wall, revealing a narrow stone passage leading west. "
                "Cool air drifts from the darkness beyond."
            )
        else:
            base += (
                " A heavy iron lever is embedded in the base of the "
                "northern bookshelf, seemingly meant to operate some "
                "hidden mechanism."
            )
        return base

    @staticmethod
    def _armory_description(state: GameState) -> str:
        """Return the Armory's description, modified by inventory."""
        base = (
            "Shelves of corroded weapons line the walls — swords, "
            "spear tips, dented shields."
        )
        if "torch" in state.inventory:
            base += (
                " Your torch casts a flickering light across the rusted "
                "metal, making the old blades gleam like living things."
            )
        else:
            base += (
                " A short torch rests on a pedestal near the entrance, "
                "unlit and patient."
            )
        base += (
            " A heavy iron door with a keyhole stands to the north, "
            "sealed shut."
        )
        if state.flags.get("armory_door_opened", False):
            base = (
                "Shelves of corroded weapons line the walls — swords, "
                "spear tips, dented shields."
            )
            if "torch" in state.inventory:
                base += (
                    " Your torch casts a flickering light across the "
                    "rusted metal."
                )
            base += " The iron door to the north hangs open now."
        return base

    # ─────────────────────────────────────────────────────────────────────
    # ITEM CALLBACKS
    # ─────────────────────────────────────────────────────────────────────

    def _use_lever(self, state: GameState) -> str:
        """Handle pulling the lever in the Library."""
        if state.current_room != "library":
            return (
                "You'd need to be in the Library to operate this lever. "
                "It's embedded in a bookshelf over there."
            )
        if state.flags.get("lever_pulled", False):
            return "You pull the lever, but it doesn't move. It's already done its job."
        state.flags["lever_pulled"] = True
        state.record("You pulled the lever. The bookshelf shuddered and slid aside.")
        return (
            "You grip the iron lever and pull with both hands. There is "
            "a deep mechanical groan, followed by the grinding of stone "
            "on stone. The northern bookshelf retracts smoothly into the "
            "wall, revealing a narrow passage leading west. Cold air "
            "wafts from the gap."
        )

    def _use_scroll(self, state: GameState) -> str:
        """Handle reading the ancient scroll — triggers the secret ending."""
        if state.flags.get("won", False):
            return (
                "The scroll's runes glow gently, as if nodding. "
                "You've already read its secret."
            )
        state.flags["won"] = True
        state.record("You read the Ancient Scroll and discovered the secret ending.")
        return (
            "You unroll the ancient scroll with trembling fingers. The "
            "glowing ink rearranges itself before your eyes, and you "
            "understand. The words are not a riddle or a map — they are "
            "a confession. The Architect built this dungeon not as a "
            "prison, but as a letter. A message to anyone brave (or "
            "lost) enough to find it:\n"
            "\n"
            "  'You came here seeking treasure, gold, or glory.\n"
            "   But the real prize was the asking, the walking,\n"
            "   the small courage it took to turn each door.\n"
            "   The dungeon was never meant to keep you in.\n"
            "   It was meant to show you that you could walk out.'\n"
            "\n"
            "The runes fade to warm gold, then to nothing. The scroll "
            "crumbles to harmless dust on your hands.\n"
            "\n"
            "═══════════════════════════════════════════════════\n"
            "  ✦  SECRET ENDING UNLOCKED  ✦\n"
            "  'The Letter of the Architect'\n"
            "═══════════════════════════════════════════════════\n"
            "\n"
            "You feel, for the first time in a long while, that the way "
            "forward is simply forward."
        )

    # ─────────────────────────────────────────────────────────────────────
    # INPUT PARSING
    # ─────────────────────────────────────────────────────────────────────

    def _parse_input(self, raw: str) -> tuple[str, list[str]]:
        """Parse raw player input into (canonical_command, arguments).

        Handles two-word commands (e.g., "look at"), partial command
        matches, and synonym resolution.

        Returns:
            A tuple of (command, args). `command` is one of the canonical
            command names; `args` is the list of remaining tokens.

        Raises:
            ValueError: If the input cannot be mapped to a known command.
        """
        tokens = raw.strip().lower().split()
        if not tokens:
            raise ValueError("empty")

        # Check for two-word commands first ("look at")
        if len(tokens) >= 2:
            two_word = f"{tokens[0]} {tokens[1]}"
            if two_word in self._COMMANDS:
                return self._COMMANDS[two_word], tokens[2:]
            # "look" followed by something that isn't "at" → treat as "look"
            # (the extra tokens become args, but "look" takes no args)

        # Single-token command with partial matching
        command = self._resolve_command(tokens[0])
        return command, tokens[1:]

    def _resolve_command(self, token: str) -> str:
        """Resolve a single token to a canonical command name.

        Uses exact match first, then prefix matching.

        Raises:
            ValueError: If no match is found.
        """
        if token in self._COMMANDS:
            return self._COMMANDS[token]

        # Prefix matching
        matches = [
            canonical
            for key, canonical in self._COMMANDS.items()
            if key.startswith(token) and len(token) >= 1
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # Disambiguate: prefer shortest key that matches
            best = min(
                (key for key in self._COMMANDS if key.startswith(token)),
                key=len,
            )
            return self._COMMANDS[best]

        # Check if it's a direction (bare direction → go)
        if token in self._DIRECTIONS:
            return "go"

        raise ValueError(token)

    def _resolve_direction(self, token: str) -> Optional[str]:
        """Resolve a token to a canonical direction.

        Returns:
            The direction string, or None if unrecognizable.
        """
        if token in self._DIRECTIONS:
            return self._DIRECTIONS[token]
        # Partial match
        matches = [
            canonical
            for key, canonical in self._DIRECTIONS.items()
            if key.startswith(token)
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def _resolve_item(self, query: str) -> Optional[Item]:
        """Find an item by partial name match in inventory or current room.

        Searches inventory first, then the current room's items.

        Returns:
            The matching Item, or None.
        """
        current_room = self.rooms[self.state.current_room]
        search_pool: list[str] = self.state.inventory + current_room.items

        for item_id in search_pool:
            item = self.items[item_id]
            if item.item_id == query or item.name.lower().startswith(query):
                return item

        # Broader: substring match
        for item_id in search_pool:
            item = self.items[item_id]
            if query in item.name.lower():
                return item

        return None

    # ─────────────────────────────────────────────────────────────────────
    # COMMAND EXECUTORS
    # ─────────────────────────────────────────────────────────────────────

    def _cmd_look(self, args: list[str]) -> str:
        """Execute the 'look' or 'look at <item>' command."""
        if args:
            query = " ".join(args)
            item = self._resolve_item(query)
            if item is None:
                return f"You don't see anything by that name here."
            return item.description

        room = self.rooms[self.state.current_room]
        desc = room.dynamic_description(self.state) if room.dynamic_description else room.description
        parts: list[str] = [desc]

        # List visible items
        visible = [self.items[iid].name for iid in room.items if iid in room.items]
        if visible:
            parts.append(f"You can see: {', '.join(visible)}.")

        # List exits
        exit_dirs = [d.capitalize() for d in room.exits.keys()]
        if exit_dirs:
            parts.append(f"Exits: {', '.join(exit_dirs)}.")

        return "\n".join(parts)

    def _cmd_go(self, args: list[str]) -> str:
        """Execute movement: go/move/walk <direction> or bare <direction>."""
        if not args:
            return "Go where? (north, south, east, west)"

        direction = self._resolve_direction(args[0])
        if direction is None:
            return f"'{args[0]}' is not a direction I understand."

        room = self.rooms[self.state.current_room]

        # Check if the direction is a valid exit
        if direction not in room.exits:
            return f"You can't go {direction} from here. There's no passage that way."

        # Check for locked doors
        lock = room.locked_doors.get(direction)
        if lock is not None:
            if self._is_lock_satisfied(lock):
                msg = lock.unlock_message
                if lock.requires == "rusted_key" or lock.requires.startswith("flag:"):
                    state_flag = lock.requires.replace("flag:", "")
                    if not state_flag.startswith("armory"):
                        state_flag = f"door_{self.state.current_room}_{direction}_opened"
                    self.state.flags[state_flag] = True
                    # For armory specifically
                    if self.state.current_room == "armory" and direction == "north":
                        self.state.flags["armory_door_opened"] = True
                target = room.exits[direction]
                self._do_move(target, msg)
                return msg
            else:
                return lock.locked_message

        # Normal movement
        target = room.exits[direction]
        self._do_move(target, None)
        return f"You head {direction}."

    def _is_lock_satisfied(self, lock: LockInfo) -> bool:
        """Check whether a lock's requirements are met.

        Supports two lock types:
          - "flag:<name>" — the named flag must be True.
          - "<item_id>"  — the item must be in the player's inventory.
        """
        if lock.requires.startswith("flag:"):
            flag_name = lock.requires[5:]
            return self.state.flags.get(flag_name, False)
        return lock.requires in self.state.inventory

    def _do_move(self, target_room_id: str, override_msg: Optional[str] = None) -> None:
        """Perform the actual room transition and record it in history."""
        self.state.current_room = target_room_id
        self.state.visited_rooms.add(target_room_id)
        msg = override_msg or f"You are now in the {self.rooms[target_room_id].name}."
        self.state.record(msg)

    def _cmd_take(self, args: list[str]) -> str:
        """Execute 'take <item>' or synonym."""
        if not args:
            return "Take what?"

        query = " ".join(args)
        item = self._resolve_item(query)
        if item is None:
            return f"You don't see '{query}' here to take."

        room = self.rooms[self.state.current_room]
        if item.item_id not in room.items:
            if item.item_id in self.state.inventory:
                return f"You're already carrying the {item.name}."
            return f"You don't see '{query}' in this room."

        # Remove from room, add to inventory
        room.items.remove(item.item_id)
        self.state.inventory.append(item.item_id)
        self.state.record(f"You took the {item.name}.")

        # Side effect: torch changes Armory description dynamically
        msg = f"You pick up the {item.name}."
        if item.item_id == "torch":
            msg += " You tuck it under your arm. It will come in handy in the dark."
        return msg

    def _cmd_drop(self, args: list[str]) -> str:
        """Execute 'drop <item>' or synonym."""
        if not args:
            return "Drop what?"

        query = " ".join(args)
        item = self._resolve_item(query)
        if item is None:
            return f"You don't have '{query}' to drop."

        if item.item_id not in self.state.inventory:
            return f"You're not carrying the {item.name}."

        self.state.inventory.remove(item.item_id)
        room = self.rooms[self.state.current_room]
        room.items.append(item.item_id)
        self.state.record(f"You dropped the {item.name}.")
        return f"You set down the {item.name}."

    def _cmd_inventory(self, args: list[str]) -> str:
        """Execute 'inventory' or 'i'."""
        if not self.state.inventory:
            return "Your pack is empty."
        parts = ["You are carrying:"]
        for item_id in self.state.inventory:
            item = self.items[item_id]
            parts.append(f"  • {item.name}")
        return "\n".join(parts)

    def _cmd_use(self, args: list[str]) -> str:
        """Execute 'use <item>'."""
        if not args:
            return "Use what?"

        query = " ".join(args)
        item = self._resolve_item(query)
        if item is None:
            return f"You don't have '{query}' to use."

        if item.item_id not in self.state.inventory:
            return f"That's not something you can use from here."

        if item.on_use is None:
            return (
                f"You fiddle with the {item.name}, but nothing happens. "
                f"It's not really meant to be 'used' in a functional sense."
            )

        result = item.on_use(self.state)
        self.state.record(f"You used the {item.name}.")
        return result

    def _cmd_help(self, args: list[str]) -> str:
        """Display the help text."""
        return (
            "═══════════ Available Commands ═══════════\n"
            "  look / l              Look around the current room\n"
            "  look at <item>        Examine an item (also: examine <item>)\n"
            "  go <direction>        Move (also: move, walk, or just the direction)\n"
            "    Directions: north, south, east, west (n, s, e, w)\n"
            "  take <item>           Pick up an item (also: grab, get)\n"
            "  drop <item>           Put down an item (also: put)\n"
            "  inventory / i         List what you're carrying\n"
            "  use <item>            Activate an item's effect\n"
            "  history               Show the last 10 events\n"
            "  help / h              Show this help\n"
            "  quit / q / exit       Leave the dungeon\n"
            "═══════════════════════════════════════════"
        )

    def _cmd_history(self, args: list[str]) -> str:
        """Show the last 10 entries in the game history."""
        if not self.state.history:
            return "Nothing has happened yet."
        recent = self.state.history[-10:]
        lines = ["─ Previous Events ─"]
        for i, entry in enumerate(recent, 1):
            lines.append(f"  {i:2d}. {entry}")
        return "\n".join(lines)

    def _cmd_quit(self, args: list[str]) -> str:
        """Return a farewell message (the engine will check for this)."""
        return "You step back out into the night air. The dungeon swallows your footsteps behind you. Goodbye."

    # ─────────────────────────────────────────────────────────────────────
    # OUTPUT RENDERING
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _banner() -> str:
        """Return the ASCII banner displayed at game start."""
        return r"""
    ╔══════════════════════════════════════════════════════════╗
    ║                                                        ║
    ║   ██████╗ ██████╗ ██████╗        ██████╗               ║
    ║  ██╔════╝██╔═══██╗██╔══██╗      ██╔═══██╗              ║
    ║  ██║     ██║   ██║██████╔╝█████╗██║   ██║████████╗    ║
    ║  ██║     ██║   ██║██╔══██╗╚════╝██║   ██║╚════██║    ║
    ║  ╚██████╗╚██████╔╝██║  ██║      ╚██████╔╝███████║    ║
    ║   ╚═════╝ ╚═════╝ ╚═╝  ╚═╝       ╚═════╝ ╚══════╝    ║
    ║                                                        ║
    ║         A  Text-Based  Adventure                       ║
    ║                                                        ║
    ╚══════════════════════════════════════════════════════════╝
"""

    @staticmethod
    def _separator() -> str:
        """Return a separator line printed between command outputs."""
        return "─" * 58

    def _render_room(self) -> str:
        """Render the current room header (name + description + exits)."""
        room = self.rooms[self.state.current_room]
        desc = room.dynamic_description(self.state) if room.dynamic_description else room.description
        parts: list[str] = [f"  ╔═ {room.name} ═{'═' * max(0, 48 - len(room.name))}"]
        # Wrap description
        words = desc.split()
        line: list[str] = []
        lines: list[str] = []
        for word in words:
            line.append(word)
            if sum(len(w) + 1 for w in line) > 54:
                lines.append("  ║ " + " ".join(line) + " " * max(0, 54 - len(" ".join(line))))
                line = [word]
        if line:
            lines.append("  ║ " + " ".join(line) + " " * max(0, 54 - len(" ".join(line))))

        # Visible items
        if room.items:
            item_names = ", ".join(self.items[iid].name for iid in room.items)
            lines.append(f"  ║   [Items: {item_names}]")
        # Exits
        exit_list = ", ".join(d.capitalize() for d in room.exits)
        lines.append(f"  ║   [Exits: {exit_list}]")
        lines.append("  ╚" + "═" * 52)
        return "\n".join(parts)

    # ─────────────────────────────────────────────────────────────────────
    # GAME LOOP
    # ─────────────────────────────────────────────────────────────────────

    def _execute_command(self, command: str, args: list[str]) -> tuple[str, bool]:
        """Route a parsed command to its handler.

        Returns:
            (output_message, should_quit)
        """
        dispatch: dict[str, Callable[[list[str]], str]] = {
            "look": self._cmd_look,
            "look_at": self._cmd_look,
            "go": self._cmd_go,
            "take": self._cmd_take,
            "drop": self._cmd_drop,
            "inventory": self._cmd_inventory,
            "use": self._cmd_use,
            "help": self._cmd_help,
            "history": self._cmd_history,
            "quit": self._cmd_quit,
        }

        handler = dispatch.get(command)
        if handler is None:
            return f"I don't understand '{command}'. Type 'help' for a list of commands.", False

        output = handler(args)
        should_quit = command == "quit"
        return output, should_quit

    def run(self) -> None:
        """Start the interactive game loop.

        Prints the banner, the starting room, and then reads commands
        until the player quits, EOF is reached, or Ctrl-C is pressed.
        """
        print(self._banner())
        print("  Welcome, traveler. The dungeon awaits.\n")
        print(self._render_room())
        print()

        self.state.record("You entered the Entrance Hall.")

        while True:
            try:
                raw = input("\n  > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n\n  The dungeon fades to silence. Goodbye, brave soul.\n")
                return

            if not raw:
                continue

            try:
                command, args = self._parse_input(raw)
            except ValueError as exc:
                if str(exc) == "empty":
                    continue
                print(f"\n  {self._separator()}")
                print(f"  I don't understand '{raw}'. Type 'help' for commands.")
                print(f"  {self._separator()}\n")
                continue

            output, should_quit = self._execute_command(command, args)

            print(f"\n  {self._separator()}")
            # Render room context for look and movement commands
            if command in ("look", "go"):
                print(self._render_room())
                print()
            print(f"  {output}")
            print(f"  {self._separator()}\n")

            if should_quit:
                print("\n  [Game Over — Thank you for playing Dungeon of Echoes]\n")
                return


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    """Entry point: create the engine and start the game loop."""
    engine: GameEngine = GameEngine()
    engine.run()


if __name__ == "__main__":
    main()
