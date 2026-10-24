"""
Dungeon Crawler — A text-based adventure game engine.

A self-contained, single-file adventure game built entirely with the Python
standard library. Features a pluggable command system, a small dungeon world
with six interconnected rooms, item interactions, and a secret ending.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Item:
    """A collectable object that may have a special on_use effect.

    Attributes:
        name: The unique identifier for the item (lowercase, e.g. "rusted key").
        description: Flavour text shown when the player examines the item.
        on_use: Optional callback invoked when the player uses the item.
                Receives the active GameState and returns a message string.
    """

    name: str
    description: str
    on_use: Optional[Callable[[GameState], str]] = None


@dataclass
class Room:
    """A location in the dungeon world.

    Attributes:
        room_id: Unique string identifier (e.g. "entrance_hall").
        name: Human-readable display name.
        description: Default description text.
        exits: Mapping of direction → target room_id.
        items: List of item IDs currently resting in this room.
        locked_exits: Directions that require a specific item ID to open.
        conditional_descriptions: Mapping of flag name → alternate description
            shown when that flag is True.
    """

    room_id: str
    name: str
    description: str
    exits: dict[str, str] = field(default_factory=dict)
    items: list[str] = field(default_factory=list)
    locked_exits: dict[str, str] = field(default_factory=dict)  # direction → required item
    conditional_descriptions: dict[str, str] = field(default_factory=dict)  # flag → description


@dataclass
class GameState:
    """Mutable state snapshot for the running game session.

    Attributes:
        current_room: The room_id where the player currently stands.
        inventory: Ordered list of item IDs the player is carrying.
        visited_rooms: Set of room_ids the player has entered.
        flags: Boolean flags tracking world events (e.g. "lever_pulled").
        history: Append-only log of every message shown to the player.
        won: True once the secret ending has been triggered.
    """

    current_room: str = "entrance_hall"
    inventory: list[str] = field(default_factory=list)
    visited_rooms: set[str] = field(default_factory=set)
    flags: dict[str, bool] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    won: bool = False

    def set_flag(self, flag: str) -> None:
        """Set a boolean world flag to True."""
        self.flags[flag] = True

    def has_flag(self, flag: str) -> bool:
        """Return True if the given flag is set."""
        return self.flags.get(flag, False)

    def has_item(self, item_id: str) -> bool:
        """Return True if the player is carrying the given item."""
        return item_id in self.inventory


# ─────────────────────────────────────────────────────────────────────────────
# World Builder
# ─────────────────────────────────────────────────────────────────────────────


def _item_use_lever(state: GameState) -> str:
    """Pull the lever in the Library, revealing the secret passage."""
    if state.current_room != "library":
        return "The lever is bolted to the wall — it only makes sense here in the Library."
    if state.has_flag("lever_pulled"):
        return "You've already pulled the lever. The bookshelf stands aside, revealing darkness beyond."
    state.set_flag("lever_pulled")
    return (
        "You grip the cold iron lever and heave. The bookshelf groans, "
        "scrapes across the stone floor, and swings open — revealing a "
        "narrow passage heading west. Cool air rushes out."
    )


def _item_use_scroll(state: GameState) -> str:
    """Read the ancient scroll to reveal the secret ending."""
    if state.has_flag("secret_revealed"):
        return "You've already read the scroll. Its wisdom is etched into your memory."
    state.set_flag("secret_revealed")
    state.won = True
    return (
        "\n  ╔══════════════════════════════════════════════════════════╗\n"
        "  ║  THE ANCIENT SCROLL UNFURLS IN YOUR HANDS...        ║\n"
        "  ║                                                    ║\n"
        "  ║  'He who seeks the golden path finds it only       ║\n"
        "  ║   when the shelf of knowledge is moved aside.     ║\n"
        "  ║   You have read what was written before the       ║\n"
        "  ║   first king drew breath. You are the heir        ║\n"
        "  ║   of this dungeon. All its treasures are yours.'  ║\n"
        "  ║                                                    ║\n"
        "  ║  ✦  SECRET ENDING UNLOCKED  ✦                     ║\n"
        "  ╚══════════════════════════════════════════════════════════╝"
    )


def _item_use_torch(state: GameState) -> str:
    """Light the torch (narrative flavour; the effect is passive on pickup)."""
    if state.has_flag("torch_lit"):
        return "The torch is already burning brightly."
    state.set_flag("torch_lit")
    return "You strike flint to the torch head. Warm amber light floods outward."


def _item_use_coin(state: GameState) -> str:
    """Attempt to use the golden coin — it's decorative."""
    return (
        "You flip the golden coin into the air. It lands heads-up with a "
        "confident *ting*. Unfortunately, there is no vending machine here. "
        "It's a coin. You are in a dungeon. The coin judges you silently."
    )


def _item_use_key(state: GameState) -> str:
    """Using the key directly just reminds you it's for doors."""
    return "You hold the rusted key up to the light. It's clearly meant for a lock, not a puzzle."


def build_world() -> tuple[dict[str, Room], dict[str, Item]]:
    """Construct and return the complete dungeon world.

    Returns:
        A tuple of (rooms_dict, items_dict) ready for the GameEngine.
    """
    # ── Items ────────────────────────────────────────────────────────────
    items: dict[str, Item] = {
        "rusted key": Item(
            name="rusted key",
            description=(
                "A small iron key, orange with rust. Its teeth are worn but "
                "still recognisable — it fits *something* around here."
            ),
            on_use=_item_use_key,
        ),
        "lever": Item(
            name="lever",
            description=(
                "A heavy iron lever jutting from the wall. It's bolted in "
                "place — you'll need to *use* it rather than carry it."
            ),
            on_use=_item_use_lever,
        ),
        "torch": Item(
            name="torch",
            description=(
                "A pitch-soaked torch, dry and ready. One spark and this "
                "place won't feel quite so oppressive."
            ),
            on_use=_item_use_torch,
        ),
        "golden coin": Item(
            name="golden coin",
            description=(
                "A gleaming gold coin stamped with a face you don't "
                "recognise. Engraved on the reverse: 'Valid in no known "
                "economy. Collector's item. Do not spend on goblins.'"
            ),
            on_use=_item_use_coin,
        ),
        "ancient scroll": Item(
            name="ancient scroll",
            description=(
                "A brittle parchment sealed with black wax. The script is "
                "archaic but legible. Use it to read its contents."
            ),
            on_use=_item_use_scroll,
        ),
    }

    # ── Rooms ────────────────────────────────────────────────────────────
    rooms: dict[str, Room] = {
        "entrance_hall": Room(
            room_id="entrance_hall",
            name="Entrance Hall",
            description=(
                "A vaulted stone chamber, damp and cold. Water drips from "
                "somewhere unseen, echoing off mossy walls. A rusted key "
                "glints on the floor near the south wall. Passages lead "
                "north, east, and south."
            ),
            exits={"north": "library", "east": "armory", "south": "garden"},
            items=["rusted key"],
        ),
        "library": Room(
            room_id="library",
            name="Library",
            description=(
                "Dust hangs in the stale air. Shelves line every wall, "
                "their spines cracked and faded. A massive bookshelf "
                "blocks what *should* be a west passage. An iron lever "
                "protrudes from the far wall."
            ),
            exits={"south": "entrance_hall", "west": "secret_chamber"},
            items=["lever"],
            locked_exits={"west": "__lever_flag__"},  # special sentinel
            conditional_descriptions={
                "lever_pulled": (
                    "The bookshelf has swung aside, revealing a narrow "
                    "passage heading west. Dust still dances in the "
                    "shaft of grey light. The shelves here are mostly "
                    "empty — whatever was stored in the chamber beyond "
                    "was taken long ago."
                ),
            },
        ),
        "armory": Room(
            room_id="armory",
            name="Armory",
            description=(
                "Rusted weapon racks line the walls, mostly empty. A "
                "single torch leans in a sconce, untouched. A heavy oak "
                "door to the north is bound with iron and sealed with a "
                "padlock. The only exit besides the door is west."
            ),
            exits={"west": "entrance_hall", "north": "treasure_room"},
            items=["torch"],
            locked_exits={"north": "rusted key"},
            conditional_descriptions={
                "torch_lit": (
                    "Flickering torchlight throws dancing shadows across "
                    "the empty racks. The northern door's padlock "
                    "catches the flame — a heavy iron lock, clearly "
                    "needing a key. West leads back to the Entrance Hall."
                ),
            },
        ),
        "treasure_room": Room(
            room_id="treasure_room",
            name="Treasure Room",
            description=(
                "A small, dry chamber. On a stone pedestal rests a single "
                "golden coin. Above it, carved into the wall in faded "
                "gold leaf, an inscription reads: 'ONE COIN, ONE KING, "
                "ONE DUNGEON — THE REST IS SILENCE.' The only exit is "
                "south."
            ),
            exits={"south": "armory"},
            items=["golden coin"],
        ),
        "secret_chamber": Room(
            room_id="secret_chamber",
            name="Secret Chamber",
            description=(
                "A hidden alcove, untouched for centuries. Cobwebs "
                "drape a single stone plinth. Upon it rests an ancient "
                "scroll in a black wax seal. The air smells of cedar "
                "and old ink. The only way out is east."
            ),
            exits={"east": "library"},
            items=["ancient scroll"],
        ),
        "garden": Room(
            room_id="garden",
            name="Garden",
            description=(
                "An impossible pocket of nature beneath the dungeon — "
                "a mossy clearing lit by pale, sourceless light. Ferns "
                "unfurl between flagstones. A small carved stone reads: "
                "'You've found peace.' The only exit is north."
            ),
            exits={"north": "entrance_hall"},
            items=[],
        ),
    }

    return rooms, items


# ─────────────────────────────────────────────────────────────────────────────
# Game Engine
# ─────────────────────────────────────────────────────────────────────────────


class GameEngine:
    """Core game loop: parses input, dispatches commands, renders output.

    Args:
        rooms: Mapping of room_id → Room.
        items: Mapping of item_id → Item.
        start_room: The room_id where the player begins.
    """

    # Separator drawn between command outputs.
    SEPARATOR: str = "─" * 60

    # Command aliases and partial-match table.
    COMMAND_SYNONYMS: dict[str, str] = {
        "examine": "look at",
        "inspect": "look at",
        "check": "look at",
        "grab": "take",
        "get": "take",
        "pick up": "take",
        "pick": "take",
        "walk": "go",
        "travel": "go",
        "move": "go",
        "i": "inventory",
        "inv": "inventory",
        "bag": "inventory",
        "h": "help",
        "?": "help",
        "q": "quit",
        "exit": "quit",
        "bye": "quit",
    }

    # Valid single-word directions.
    DIRECTIONS: list[str] = ["north", "south", "east", "west"]

    # Direction abbreviations.
    DIRECTION_SHORT: dict[str, str] = {
        "n": "north",
        "s": "south",
        "e": "east",
        "w": "west",
    }

    def __init__(self, rooms: dict[str, Room], items: dict[str, Item], start_room: str = "entrance_hall") -> None:
        self.rooms: dict[str, Room] = rooms
        self.items: dict[str, Item] = items
        self.state: GameState = GameState(current_room=start_room)

    # ── Public API ───────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the interactive game loop. Handles EOF and interrupts."""
        self._print_banner()
        self.state.visited_rooms.add(self.state.current_room)
        self._record("You awaken in the Entrance Hall.")
        self._print_output(self._describe_room())

        while True:
            try:
                raw = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                self._print_output("Farewell, adventurer. The dungeon waits for your return.")
                break

            if not raw:
                continue

            output = self._process_command(raw)
            if output is None:
                break  # quit was called
            self._print_output(output)

    # ── Input Parsing ────────────────────────────────────────────────────

    def _process_command(self, raw: str) -> Optional[str]:
        """Parse and execute a single player command.

        Returns the output string, or None if the game should exit.
        """
        text = raw.lower().strip()

        # Try multi-word synonyms first (e.g. "pick up sword").
        for synonym, canonical in sorted(self.COMMAND_SYNONYMS.items(), key=lambda x: -len(x[0])):
            if text == synonym or text.startswith(synonym + " "):
                remainder = text[len(synonym):].strip()
                return self._dispatch(canonical, remainder)

        # Single-word commands with partial matching.
        parts = text.split(maxsplit=1)
        verb = parts[0]
        remainder = parts[1] if len(parts) > 1 else ""

        # Bare direction shortcuts: "n", "north", "go north", "move east".
        if verb in self.DIRECTION_SHORT:
            return self._cmd_go(self.DIRECTION_SHORT[verb])
        if verb in self.DIRECTIONS:
            return self._cmd_go(verb)

        # Try partial match against known commands.
        known_verbs = [
            "look", "go", "take", "drop", "inventory",
            "use", "help", "history", "quit",
        ]
        matched_verb = self._partial_match(verb, known_verbs)

        if matched_verb is None:
            return f"I don't understand '{raw}'. Type 'help' for a list of commands."

        # Special: bare "look" vs "look at <item>".
        if matched_verb == "look":
            if remainder.startswith("at "):
                remainder = remainder[3:]
            if remainder:
                return self._cmd_look_at(remainder)
            return self._cmd_look()

        dispatch_map: dict[str, Callable[[str], str]] = {
            "go": self._cmd_go,
            "take": self._cmd_take,
            "drop": self._cmd_drop,
            "inventory": self._cmd_inventory,
            "use": self._cmd_use,
            "help": self._cmd_help,
            "history": self._cmd_history,
            "quit": self._cmd_quit,
        }

        handler = dispatch_map[matched_verb]
        return handler(remainder)

    def _dispatch(self, canonical: str, remainder: str) -> str:
        """Route a resolved canonical command to its handler."""
        if canonical == "look at":
            return self._cmd_look_at(remainder)
        if canonical == "take":
            return self._cmd_take(remainder)
        if canonical == "go":
            # "move <dir>" or "walk <dir>" → go <dir>
            if remainder in self.DIRECTION_SHORT:
                remainder = self.DIRECTION_SHORT[remainder]
            return self._cmd_go(remainder)
        if canonical == "inventory":
            return self._cmd_inventory("")
        if canonical == "help":
            return self._cmd_help("")
        if canonical == "quit":
            # Return None via _cmd_quit but _dispatch must return str, so:
            return self._cmd_quit("")
        return f"Unknown command '{canonical}'."

    @staticmethod
    def _partial_match(word: str, candidates: list[str]) -> Optional[str]:
        """Return the unique candidate that starts with *word*, or None."""
        matches = [c for c in candidates if c.startswith(word)]
        if len(matches) == 1:
            return matches[0]
        return None

    # ── Command Handlers ─────────────────────────────────────────────────

    def _cmd_look(self) -> str:
        """Handle the 'look' command — describe the current room."""
        return self._describe_room()

    def _cmd_look_at(self, target: str) -> str:
        """Handle 'look at <item>' — show an item's description."""
        item = self._resolve_item(target)
        if item is None:
            return f"You don't see any '{target}' here."
        return f"[{item.name.title()}]\n{item.description}"

    def _cmd_go(self, direction: str) -> str:
        """Handle 'go <direction>' — move the player to an adjacent room."""
        if direction in self.DIRECTION_SHORT:
            direction = self.DIRECTION_SHORT[direction]
        if direction not in self.DIRECTIONS:
            return f"'{direction}' is not a valid direction. Try north, south, east, or west."

        room = self.rooms[self.state.current_room]

        if direction not in room.exits:
            return f"There is no passage to the {direction}."

        # Check for locked exits.
        if direction in room.locked_exits:
            req = room.locked_exits[direction]
            if req == "__lever_flag__":
                if not self.state.has_flag("lever_pulled"):
                    return (
                        "The passage to the west is blocked by a massive "
                        "bookshelf. Perhaps there's a mechanism nearby?"
                    )
            else:
                # Standard item-based lock: check if player holds the item.
                if not self.state.has_item(req):
                    return (
                        f"The door to the {direction} is locked. "
                        f"You need a '{req}' to open it."
                    )
                # Auto-use the key.
                self.state.inventory.remove(req)
                msg = f"✦ You unlock the door with the {req}. The lock clicks open.\n"
                self._record(f"Unlocked {direction} door using {req}.")
                target_room = room.exits[direction]
                self.state.current_room = target_room
                self.state.visited_rooms.add(target_room)
                return msg + self._describe_room()

        # Normal movement.
        target_room = room.exits[direction]
        self.state.current_room = target_room
        self.state.visited_rooms.add(target_room)
        self._record(f"Moved {direction} to {self.rooms[target_room].name}.")
        return self._describe_room()

    def _cmd_take(self, target: str) -> str:
        """Handle 'take <item>' — add an item from the room to inventory."""
        if not target:
            return "Take what? Specify an item, e.g. 'take torch'."

        room = self.rooms[self.state.current_room]
        # Fuzzy-match against room items.
        matched = self._match_in_list(target, room.items)
        if matched is None:
            return f"You don't see any '{target}' in the {room.name}."

        room.items.remove(matched)
        self.state.inventory.append(matched)
        self._record(f"Picked up {matched}.")

        # Special passive effect: torch changes armory description.
        if matched == "torch":
            self.state.set_flag("torch_lit")
            return f"You pick up the torch. {self.items[matched].description}\n(The armory feels less oppressive now.)"

        return f"You take the {matched}."

    def _cmd_drop(self, target: str) -> str:
        """Handle 'drop <item>' — remove an item from inventory to the room."""
        if not target:
            return "Drop what? Specify an item."

        matched = self._match_in_list(target, self.state.inventory)
        if matched is None:
            return f"You're not carrying '{target}'."

        self.state.inventory.remove(matched)
        self.rooms[self.state.current_room].items.append(matched)
        self._record(f"Dropped {matched}.")
        return f"You drop the {matched}."

    def _cmd_inventory(self, _arg: str) -> str:
        """Handle 'inventory' / 'i' — list carried items."""
        if not self.state.inventory:
            return "Your hands are empty."
        lines = ["You are carrying:"]
        for item_id in self.state.inventory:
            lines.append(f"  • {item_id}")
        return "\n".join(lines)

    def _cmd_use(self, target: str) -> str:
        """Handle 'use <item>' — invoke the item's on_use callback."""
        if not target:
            return "Use what? Specify an item."

        matched = self._match_in_list(target, self.state.inventory)
        if matched is None:
            # Allow using items in the room (e.g. the lever).
            room = self.rooms[self.state.current_room]
            matched = self._match_in_list(target, room.items)
            if matched is None:
                return f"You don't have or see a '{target}'."

        item = self.items.get(matched)
        if item is None:
            return f"The {matched} does nothing interesting."

        if item.on_use is None:
            return f"You can't do anything useful with the {matched}."

        result = item.on_use(self.state)
        self._record(f"Used {matched}.")
        return result

    def _cmd_help(self, _arg: str) -> str:
        """Handle 'help' — print available commands."""
        return (
            "AVAILABLE COMMANDS:\n"
            "  look / l              — describe current room\n"
            "  look at <item>        — examine an item\n"
            "  go <direction>        — move (n/s/e/w also work)\n"
            "  take <item>           — pick up an item\n"
            "  drop <item>           — drop a held item\n"
            "  inventory / i         — list carried items\n"
            "  use <item>            — activate an item's effect\n"
            "  history               — show last 10 events\n"
            "  help / h / ?          — this message\n"
            "  quit / q              — leave the dungeon\n"
            "\n"
            "Directions: north, south, east, west (or n, s, e, w)\n"
            "Synonyms: examine = look at, grab/get = take, walk/move = go"
        )

    def _cmd_history(self, _arg: str) -> str:
        """Handle 'history' — print the last 10 recorded events."""
        recent = self.state.history[-10:]
        if not recent:
            return "No history yet."
        lines = ["── Recent Events ──"]
        for i, entry in enumerate(recent, 1):
            lines.append(f"  {i:2d}. {entry}")
        return "\n".join(lines)

    def _cmd_quit(self, _arg: str) -> str:
        """Handle 'quit' — end the session gracefully."""
        if self.state.won:
            msg = (
                "\n  ✦ CONGRATULATIONS! You completed the dungeon and "
                "unlocked the secret ending. ✦\n\n"
                "Farewell, heir of the dungeon. The scroll's words "
                "follow you into the light."
            )
        else:
            msg = "You leave the dungeon. Perhaps another time, you'll uncover its secrets."
        self._record("Quit the game.")
        return msg  # Returning a string; run() will print it. We signal quit via return.

    # ── Output / Rendering ───────────────────────────────────────────────

    def _print_banner(self) -> None:
        """Print the stylised ASCII title banner."""
        banner = r"""
    ╔═══════════════════════════════════════════════════╗
    ║  ░▒▓█ D U N G E O N   C R A W L E R █▓▒░        ║
    ║  ─────────────────────────────────────────        ║
    ║  A text adventure in six rooms.                 ║
    ║  Find the scroll. Read it. Escape.              ║
    ║  ─────────────────────────────────────────        ║
    ║  Type 'help' to see commands.                   ║
    ╚═══════════════════════════════════════════════════╝
"""
        print(banner)

    def _print_output(self, text: str) -> None:
        """Print a separator line followed by the command result."""
        print(f"\n{self.SEPARATOR}")
        print(text)
        print(self.SEPARATOR)

    def _describe_room(self) -> str:
        """Build the description string for the player's current room."""
        room = self.rooms[self.state.current_room]

        # Check conditional descriptions (e.g. after lever pulled).
        desc = room.description
        for flag, alt_desc in room.conditional_descriptions.items():
            if self.state.has_flag(flag):
                desc = alt_desc
                break

        lines = [f"═══ {room.name} ═══", "", desc]

        # Visible items.
        if room.items:
            lines.append("")
            lines.append("You can see:")
            for item_id in room.items:
                lines.append(f"  • {item_id}")

        # Available exits.
        lines.append("")
        exits_str = ", ".join(sorted(room.exits.keys()))
        lines.append(f"Exits: {exits_str}")

        # Indicate locked exits.
        locked_info: list[str] = []
        for direction in room.locked_exits:
            req = room.locked_exits[direction]
            if req == "__lever_flag__":
                if not self.state.has_flag("lever_pulled"):
                    locked_info.append(f"  {direction} (blocked)")
            else:
                if not self.state.has_item(req):
                    locked_info.append(f"  {direction} (locked — needs {req})")
        if locked_info:
            lines.append("Blocked:")
            lines.extend(locked_info)

        return "\n".join(lines)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _resolve_item(self, query: str) -> Optional[Item]:
        """Find an item by fuzzy name match in the current room or inventory."""
        room = self.rooms[self.state.current_room]
        all_visible = room.items + self.state.inventory
        matched = self._match_in_list(query, all_visible)
        if matched:
            return self.items.get(matched)
        return None

    @staticmethod
    def _match_in_list(query: str, candidates: list[str]) -> Optional[str]:
        """Return the first candidate whose name contains *query* (substring)."""
        q = query.lower().strip()
        # Exact match first.
        for c in candidates:
            if c == q:
                return c
        # Substring / prefix match.
        for c in candidates:
            if q in c or c.startswith(q):
                return c
        return None

    def _record(self, event: str) -> None:
        """Append an event to the game history log."""
        self.state.history.append(event)


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    """Build the world, instantiate the engine, and start the game loop."""
    rooms, items = build_world()
    engine = GameEngine(rooms=rooms, items=items, start_room="entrance_hall")
    engine.run()


if __name__ == "__main__":
    main()
