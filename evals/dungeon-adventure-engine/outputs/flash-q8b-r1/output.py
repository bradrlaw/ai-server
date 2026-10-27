#!/usr/bin/env python3
"""
Text-Based Adventure Game Engine
=================================
A self-contained dungeon crawler engine with a small playable world.
Uses only the Python standard library.

Run directly:  python adventure.py
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
    """Represents a collectible/usable item in the game world.

    Attributes:
        name: Unique lowercase identifier (e.g. "rusted key").
        description: Flavor text shown when the player examines the item.
        on_use: Optional callback invoked when the player uses the item.
                Receives the current GameState and returns a response string.
    """

    name: str
    description: str
    on_use: Optional[Callable[["GameState"], str]] = None


@dataclass
class Room:
    """A single location in the dungeon world.

    Attributes:
        room_id: Unique string identifier for this room.
        name: Human-readable room name.
        description: Base description of the room.
        dynamic_description: Optional callable that returns a modified
                             description based on game state (e.g. lever pulled).
        exits: Mapping of direction → target room_id.
        items: List of item IDs currently present in this room.
        locked_exits: Mapping of direction → required item name to unlock.
    """

    room_id: str
    name: str
    description: str
    dynamic_description: Optional[Callable[["GameState"], str]] = None
    exits: dict[str, str] = field(default_factory=dict)
    items: list[str] = field(default_factory=list)
    locked_exits: dict[str, str] = field(default_factory=dict)

    def get_description(self, state: "GameState") -> str:
        """Return the room's description, using dynamic variant if available."""
        if self.dynamic_description is not None:
            return self.dynamic_description(state)
        return self.description


@dataclass
class GameState:
    """Tracks all mutable state for a single playthrough.

    Attributes:
        current_room: ID of the room the player currently occupies.
        inventory: List of item IDs the player is carrying.
        visited_rooms: Set of room IDs the player has entered.
        flags: Boolean flags for tracking world events (e.g. "lever_pulled").
        history: Ordered log of all messages/events for the player.
        won: Whether the player has achieved the secret ending.
    """

    current_room: str = "entrance_hall"
    inventory: list[str] = field(default_factory=list)
    visited_rooms: set[str] = field(default_factory=set)
    flags: dict[str, bool] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    won: bool = False

    def add_history(self, message: str) -> None:
        """Append a message to the game history log."""
        self.history.append(message)

    def get_last_events(self, count: int = 10) -> list[str]:
        """Return the most recent *count* events from history."""
        return self.history[-count:]


# ─────────────────────────────────────────────────────────────────────────────
# Game Engine
# ─────────────────────────────────────────────────────────────────────────────


class GameEngine:
    """Central engine that ties together the world, state, and player input.

    Responsibilities:
        - Parse and normalize player commands.
        - Route commands to handler methods.
        - Mutate GameState and produce output strings.
    """

    # Synonyms that map alternative words to canonical command names.
    SYNONYMS: dict[str, str] = {
        "examine": "look",
        "inspect": "look",
        "check": "look",
        "grab": "take",
        "get": "take",
        "pick": "take",
        "pick up": "take",
        "put": "drop",
        "discard": "drop",
        "throw": "drop",
        "bag": "inventory",
        "inv": "inventory",
        "i": "inventory",
        "n": "north",
        "s": "south",
        "e": "east",
        "w": "west",
        "move": "go",
        "walk": "go",
        "travel": "go",
        "activate": "use",
        "pull": "use",
        "read": "use",
        "exit": "quit",
        "q": "quit",
        "h": "help",
        "?": "help",
    }

    VALID_DIRECTIONS: set[str] = {"north", "south", "east", "west"}

    COMMANDS: list[str] = [
        "look", "go", "take", "drop", "inventory",
        "use", "help", "history", "quit",
    ]

    def __init__(self, rooms: dict[str, Room], items: dict[str, Item]) -> None:
        """Initialize the engine with a world definition.

        Args:
            rooms: Mapping of room_id → Room instance.
            items: Mapping of item_id → Item instance.
        """
        self.rooms: dict[str, Room] = rooms
        self.items: dict[str, Item] = items
        self.state: GameState = GameState()
        self.running: bool = True
        self._separator: str = "─" * 60

    # ── Main Loop ──────────────────────────────────────────────────────────

    def run(self) -> None:
        """Run the interactive game loop until the player quits."""
        self._print_banner()
        # Record initial room visit
        self._enter_room(self.state.current_room, initial=True)

        while self.running:
            try:
                raw_input: str = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                self._goodbye()
                break

            if not raw_input:
                continue

            output: str = self.process_command(raw_input)
            print(self._separator)
            print(output)
            self.state.add_history(f"> {raw_input}  →  {output.split(chr(10))[0]}")

    # ── Command Parsing & Routing ──────────────────────────────────────────

    def process_command(self, raw: str) -> str:
        """Parse raw player input and dispatch to the appropriate handler.

        Args:
            raw: The raw string entered by the player.

        Returns:
            A formatted response string.
        """
        normalized: str = raw.lower().strip()
        tokens: list[str] = normalized.split()

        if not tokens:
            return "Say something, adventurer."

        # Resolve synonyms on the first token (or two-token phrases like "pick up")
        command, argument = self._resolve_command(tokens)

        # Route to handler
        handler_map: dict[str, Callable[[str], str]] = {
            "look": self._cmd_look,
            "go": self._cmd_go,
            "take": self._cmd_take,
            "drop": self._cmd_drop,
            "inventory": self._cmd_inventory,
            "use": self._cmd_use,
            "help": self._cmd_help,
            "history": self._cmd_history,
            "quit": self._cmd_quit,
        }

        handler: Optional[Callable[[str], str]] = handler_map.get(command)
        if handler is None:
            return self._unknown_command(command)

        return handler(argument)

    def _resolve_command(self, tokens: list[str]) -> tuple[str, str]:
        """Resolve raw tokens into (command, argument).

        Handles synonyms, partial matching, and bare direction words.

        Args:
            tokens: Lowercased list of words from player input.

        Returns:
            Tuple of (canonical_command, argument_string).
        """
        # Two-word synonyms like "pick up"
        if len(tokens) >= 2:
            two_word: str = f"{tokens[0]} {tokens[1]}"
            if two_word in self.SYNONYMS:
                return self.SYNONYMS[two_word], " ".join(tokens[2:])

        first: str = tokens[0]
        argument: str = " ".join(tokens[1:])

        # Direct synonym lookup
        if first in self.SYNONYMS:
            resolved: str = self.SYNONYMS[first]
            # If synonym resolves to a direction, treat as "go <direction>"
            if resolved in self.VALID_DIRECTIONS:
                return "go", resolved
            # If synonym resolves to "look" and there's an argument, it's "look at"
            if resolved == "look" and argument:
                return "look", argument
            return resolved, argument

        # Bare direction words (e.g. "north", "n")
        if first in self.VALID_DIRECTIONS:
            return "go", first
        if first in ("n", "s", "e", "w"):
            direction_map: dict[str, str] = {"n": "north", "s": "south", "e": "east", "w": "west"}
            return "go", direction_map[first]

        # Partial match against known commands
        matches: list[str] = [c for c in self.COMMANDS if c.startswith(first)]
        if len(matches) == 1:
            return matches[0], argument

        # No match — return raw token as unknown
        return first, argument

    # ── Command Handlers ───────────────────────────────────────────────────

    def _cmd_look(self, argument: str) -> str:
        """Handle 'look' and 'look at <item>' commands.

        Args:
            argument: Optional item name to examine.

        Returns:
            Room description or item description.
        """
        room: Room = self.rooms[self.state.current_room]

        if argument:
            return self._look_at_item(argument)

        # Full room description
        lines: list[str] = []
        lines.append(f"═══ {room.name} ═══")
        lines.append(room.get_description(self.state))
        lines.append("")

        # Visible items
        visible_items: list[str] = [i for i in room.items if i in self.items]
        if visible_items:
            item_names: str = ", ".join(self.items[i].name for i in visible_items)
            lines.append(f"You can see: {item_names}")
        else:
            lines.append("You see nothing of interest here.")

        # Available exits
        exits_str: str = ", ".join(room.exits.keys())
        lines.append(f"Exits: {exits_str}")

        return "\n".join(lines)

    def _look_at_item(self, name: str) -> str:
        """Examine a specific item by name.

        Args:
            name: Partial or full item name.

        Returns:
            Item description or error message.
        """
        item: Optional[Item] = self._find_item(name)
        if item is None:
            return f"I don't see '{name}' here or in your inventory."

        lines: list[str] = [f"── {item.name.title()} ──", item.description]
        if item.on_use:
            lines.append("(You could use this.)")
        return "\n".join(lines)

    def _cmd_go(self, direction: str) -> str:
        """Handle movement in a given direction.

        Args:
            direction: One of north/south/east/west.

        Returns:
            Movement confirmation or error.
        """
        if direction not in self.VALID_DIRECTIONS:
            return f"'{direction}' is not a valid direction. Try: north, south, east, west."

        room: Room = self.rooms[self.state.current_room]

        # Check if exit exists
        if direction not in room.exits:
            return f"There is no {direction} exit from here."

        # Check if exit is locked
        if direction in room.locked_exits:
            required_item: str = room.locked_exits[direction]
            if required_item in self.state.inventory:
                # Auto-use the key: unlock and proceed
                del room.locked_exits[direction]
                self.state.add_history(f"Used {required_item} to unlock the {direction} door.")
                return (
                    f"You insert the {required_item} into the lock. Click! "
                    f"The {direction} door swings open.\n\n"
                    + self._enter_room(room.exits[direction])
                )
            else:
                return (
                    f"The {direction} door is locked. "
                    f"You need a {required_item} to open it."
                )

        target_id: str = room.exits[direction]
        return self._enter_room(target_id)

    def _cmd_take(self, name: str) -> str:
        """Pick up an item from the current room.

        Args:
            name: Partial or full item name.

        Returns:
            Confirmation or error.
        """
        if not name:
            return "Take what? Specify an item."

        room: Room = self.rooms[self.state.current_room]
        item: Optional[Item] = self._find_item_in_room(name, room)

        if item is None:
            return f"There is no '{name}' here to take."

        # Remove from room, add to inventory
        room.items.remove(item.name)
        self.state.inventory.append(item.name)

        # Special: torch taken triggers Armory description change
        if item.name == "torch":
            self.state.flags["torch_taken"] = True

        return f"You pick up the {item.name}."

    def _cmd_drop(self, name: str) -> str:
        """Drop an item from inventory into the current room.

        Args:
            name: Partial or full item name.

        Returns:
            Confirmation or error.
        """
        if not name:
            return "Drop what? Specify an item."

        item: Optional[Item] = self._find_item_in_inventory(name)
        if item is None:
            return f"You're not carrying '{name}'."

        self.state.inventory.remove(item.name)
        room: Room = self.rooms[self.state.current_room]
        room.items.append(item.name)

        return f"You drop the {item.name}."

    def _cmd_inventory(self, _argument: str) -> str:
        """List items in the player's inventory.

        Returns:
            Inventory listing.
        """
        if not self.state.inventory:
            return "Your hands are empty."

        lines: list[str] = ["═══ Inventory ═══"]
        for item_id in self.state.inventory:
            item: Item = self.items[item_id]
            lines.append(f"  • {item.name}")
        return "\n".join(lines)

    def _cmd_use(self, name: str) -> str:
        """Activate an item's on_use effect.

        Args:
            name: Partial or full item name.

        Returns:
            Result of the item's use effect.
        """
        if not name:
            return "Use what? Specify an item."

        item: Optional[Item] = self._find_item_in_inventory(name)
        if item is None:
            return f"You don't have '{name}' in your inventory."

        if item.on_use is None:
            return f"The {item.name} doesn't seem to do anything when used."

        result: str = item.on_use(self.state)

        # Check for win condition
        if self.state.won:
            result += "\n\n" + self._victory_message()
            self.running = False

        return result

    def _cmd_help(self, _argument: str) -> str:
        """Print the list of available commands.

        Returns:
            Formatted help text.
        """
        lines: list[str] = [
            "═══ Commands ═══",
            "  look              — Look around the current room",
            "  look at <item>    — Examine an item",
            "  go <direction>    — Move (north/south/east/west)",
            "  <direction>       — Shorthand: n, s, e, w",
            "  take <item>       — Pick up an item",
            "  drop <item>       — Drop an item",
            "  inventory / i     — List carried items",
            "  use <item>        — Use/activate an item",
            "  history           — Show last 10 events",
            "  help              — This message",
            "  quit              — Leave the dungeon",
            "",
            "Synonyms: examine=look, grab=take, pull=use, read=use",
        ]
        return "\n".join(lines)

    def _cmd_history(self, _argument: str) -> str:
        """Display the last 10 game events.

        Returns:
            Numbered list of recent events.
        """
        events: list[str] = self.state.get_last_events(10)
        if not events:
            return "No history yet."

        lines: list[str] = ["═══ History ═══"]
        for i, event in enumerate(events, 1):
            lines.append(f"  {i:2d}. {event}")
        return "\n".join(lines)

    def _cmd_quit(self, _argument: str) -> str:
        """Gracefully end the game session.

        Returns:
            Farewell message.
        """
        self.running = False
        return "You leave the dungeon. Until next time, adventurer!"

    # ── Helper Methods ─────────────────────────────────────────────────────

    def _enter_room(self, room_id: str, initial: bool = False) -> str:
        """Transition the player into a room and return its description.

        Args:
            room_id: Target room identifier.
            initial: If True, suppress the 'You enter...' prefix.

        Returns:
            Formatted room entry text.
        """
        self.state.current_room = room_id
        self.state.visited_rooms.add(room_id)

        room: Room = self.rooms[room_id]
        lines: list[str] = []
        if not initial:
            lines.append(f"You enter the {room.name}.")
            lines.append("")
        lines.append(f"═══ {room.name} ═══")
        lines.append(room.get_description(self.state))
        lines.append("")

        visible_items: list[str] = [i for i in room.items if i in self.items]
        if visible_items:
            item_names: str = ", ".join(self.items[i].name for i in visible_items)
            lines.append(f"You can see: {item_names}")

        exits_str: str = ", ".join(room.exits.keys())
        lines.append(f"Exits: {exits_str}")

        return "\n".join(lines)

    def _find_item(self, name: str) -> Optional[Item]:
        """Find an item by partial name in current room or inventory.

        Args:
            name: Partial or full item name (lowercase).

        Returns:
            Matching Item or None.
        """
        room: Room = self.rooms[self.state.current_room]
        # Search room first, then inventory
        for item_id in room.items + self.state.inventory:
            item: Item = self.items[item_id]
            if name in item.name:
                return item
        return None

    def _find_item_in_room(self, name: str, room: Room) -> Optional[Item]:
        """Find an item by partial name in a specific room.

        Args:
            name: Partial or full item name.
            room: The Room to search.

        Returns:
            Matching Item or None.
        """
        for item_id in room.items:
            item: Item = self.items[item_id]
            if name in item.name:
                return item
        return None

    def _find_item_in_inventory(self, name: str) -> Optional[Item]:
        """Find an item by partial name in the player's inventory.

        Args:
            name: Partial or full item name.

        Returns:
            Matching Item or None.
        """
        for item_id in self.state.inventory:
            item: Item = self.items[item_id]
            if name in item.name:
                return item
        return None

    def _unknown_command(self, command: str) -> str:
        """Generate a helpful message for unrecognized commands.

        Args:
            command: The unrecognized token.

        Returns:
            Guidance message.
        """
        return (
            f"I don't understand '{command}'. "
            f"Type 'help' for a list of commands."
        )

    def _victory_message(self) -> str:
        """Return the grand victory banner shown upon winning.

        Returns:
            Multi-line victory text.
        """
        return (
            "\n"
            "╔══════════════════════════════════════════════════════════╗\n"
            "║                                                      ║\n"
            "║   ★  ★  ★  V I C T O R Y  ★  ★  ★                  ║\n"
            "║                                                      ║\n"
            "║   You have uncovered the ancient secret and          ║\n"
            "║   transcended the dungeon's curse. The scroll's      ║\n"
            "║   final words echo: \"The seeker becomes the          ║\n"
            "║   light.\" You fade into legend.                      ║\n"
            "║                                                      ║\n"
            "╚══════════════════════════════════════════════════════════╝"
        )

    # ── Display Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _print_banner() -> None:
        """Print the ASCII art title banner at game start."""
        banner: str = (
            "\n"
            "╔══════════════════════════════════════════════════════════╗\n"
            "║                                                        ║\n"
            "║     ██████╗  ██████╗ ██████╗ ███████╗███████╗██████╗   ║\n"
            "║     ██╔══██╗██╔═══██╗██╔══██╗██╔════╝██╔════╝██╔══██╗  ║\n"
            "║     ██████╔╝██║   ██║██║  ██║█████╗  █████╗  ██████╔╝  ║\n"
            "║     ██╔══██╗██║   ██║██║  ██║██╔══╝  ██╔══╝  ██╔══██╗  ║\n"
            "║     ██████╔╝╚██████╔╝██████╔╝███████╗███████╗██║  ██║  ║\n"
            "║     ╚═════╝  ╚═════╝ ╚═════╝ ╚══════╝╚══════╝╚═╝  ╚═╝  ║\n"
            "║                                                        ║\n"
            "║        ~ The Forgotten Dungeon ~                       ║\n"
            "║                                                        ║\n"
            "║   Type 'help' for commands.  Type 'quit' to leave.     ║\n"
            "╚══════════════════════════════════════════════════════════╝\n"
        )
        print(banner)

    @staticmethod
    def _goodbye() -> None:
        """Print a farewell message on unexpected exit."""
        print("\n\nYou vanish into the shadows. Farewell, adventurer. 👋")


# ─────────────────────────────────────────────────────────────────────────────
# World Builder
# ─────────────────────────────────────────────────────────────────────────────


def build_dungeon() -> tuple[dict[str, Room], dict[str, Item]]:
    """Construct and return the complete dungeon world.

    Returns:
        Tuple of (rooms_dict, items_dict).
    """

    # ── Items ──────────────────────────────────────────────────────────────

    def use_lever(state: GameState) -> str:
        """Pull the lever in the Library to reveal the secret passage."""
        if state.flags.get("lever_pulled"):
            return "The lever is already pulled. The bookshelf sits aside, revealing a dark passage."
        state.flags["lever_pulled"] = True
        return (
            "You grip the cold iron lever and pull with all your might. "
            "A grinding roar fills the room as the dusty bookshelf slides "
            "aside, revealing a narrow passage leading WEST. "
            "Dust cascades from the ceiling. The path is open."
        )

    def use_scroll(state: GameState) -> str:
        """Read the ancient scroll to trigger the secret ending."""
        state.won = True
        return (
            "You unroll the brittle ancient scroll. The ink glows faintly.\n\n"
            "  \"He who seeks beyond the walls of gold and steel,\n"
            "   who reads what the world has tried to seal,\n"
            "   shall find that the dungeon was never a cage—\n"
            "   but a door. And you, dear seeker, have turned the page.\"\n\n"
            "The words burn themselves into your memory. The dungeon trembles."
        )

    items: dict[str, Item] = {
        "rusted key": Item(
            name="rusted key",
            description=(
                "A small iron key, orange with rust. It still looks functional. "
                "The teeth are worn but the bow is shaped like a tiny shield."
            ),
        ),
        "lever": Item(
            name="lever",
            description=(
                "A heavy iron lever mounted into the wall, half-hidden behind "
                "a crumbling tapestry. It looks like it hasn't been moved in decades."
            ),
            on_use=use_lever,
        ),
        "torch": Item(
            name="torch",
            description=(
                "A sturdy wooden torch with a pitch-soaked head. It's unlit, "
                "but you find a flint beside it. Perfect for dark corridors."
            ),
        ),
        "golden coin": Item(
            name="golden coin",
            description=(
                "A gleaming gold coin stamped with the face of a long-dead king. "
                "On the reverse, it reads: 'THIS COIN HAS NO FUNCTION. "
                "ENJOY ITS SHINY EXISTENCE.' You suspect the king had a sense of humor."
            ),
        ),
        "ancient scroll": Item(
            name="ancient scroll",
            description=(
                "A scroll of impossibly thin parchment, sealed with wax that "
                "crumbles at your touch. Strange symbols shift when you're not "
                "looking directly at them. You feel compelled to read it."
            ),
            on_use=use_scroll,
        ),
    }

    # ── Rooms ──────────────────────────────────────────────────────────────

    def library_description(state: GameState) -> str:
        """Dynamic description for the Library room."""
        base: str = (
            "Dust motes dance in the pale light filtering through cracked "
            "stained glass. Shelves line every wall, their contents long "
            "since rotted to mulch. A massive bookshelf dominates the "
            "south wall, immovable as a mountain. "
        )
        if state.flags.get("lever_pulled"):
            base += (
                "The bookshelf has been dragged aside, revealing a narrow "
                "stone passage leading WEST into darkness."
            )
        else:
            base += (
                "Something glints behind a torn tapestry on the east wall—"
            )
        return base

    def armory_description(state: GameState) -> str:
        """Dynamic description for the Armory room."""
        base: str = (
            "Rusted weapons hang from hooks along the walls—swords, axes, "
            "and shields, all too corroded to be of use. A heavy oak door "
            "to the NORTH is bound in iron and sealed with a keyhole. "
        )
        if state.flags.get("torch_taken"):
            base += (
                "Your torch casts a flickering light across the room, "
                "making the hanging weapons sway like ghosts."
            )
        else:
            base += "The room is dim and cold."
        return base

    rooms: dict[str, Room] = {
        "entrance_hall": Room(
            room_id="entrance_hall",
            name="Entrance Hall",
            description=(
                "A vast, echoing hall. Pillars of cracked stone stretch into "
                "shadow above. The air smells of damp earth and old iron. "
                "Three corridors branch from here: one NORTH, one EAST, "
                "and one SOUTH toward a faint green glow."
            ),
            exits={"north": "library", "east": "armory", "south": "garden"},
            items=["rusted key"],
        ),
        "library": Room(
            room_id="library",
            name="Library",
            description=(
                "Dust motes dance in the pale light filtering through cracked "
                "stained glass. Shelves line every wall, their contents long "
                "since rotted to mulch. A massive bookshelf dominates the "
                "south wall, immovable as a mountain. Something glints behind "
                "a torn tapestry on the east wall—"
            ),
            dynamic_description=library_description,
            exits={"south": "entrance_hall", "west": "secret_chamber"},
            items=["lever"],
            locked_exits={"west": "lever"},  # Locked until lever is pulled
        ),
        "armory": Room(
            room_id="armory",
            name="Armory",
            description=(
                "Rusted weapons hang from hooks along the walls—swords, axes, "
                "and shields, all too corroded to be of use. A heavy oak door "
                "to the NORTH is bound in iron and sealed with a keyhole. "
                "The room is dim and cold."
            ),
            dynamic_description=armory_description,
            exits={"west": "entrance_hall", "north": "treasure_room"},
            items=["torch"],
            locked_exits={"north": "rusted key"},
        ),
        "treasure_room": Room(
            room_id="treasure_room",
            name="Treasure Room",
            description=(
                "A circular chamber lined with empty chests and scattered "
                "jewels. At the center, a single golden coin rests on a "
                "stone pedestal. Above it, carved into the domed ceiling, "
                "is a riddle: 'I am sought but never kept; I am spoken but "
                "never said; I am alive though I have never been fed. "
                "What am I?' (Answer: hope.)"
            ),
            exits={"south": "armory"},
            items=["golden coin"],
        ),
        "secret_chamber": Room(
            room_id="secret_chamber",
            name="Secret Chamber",
            description=(
                "A small, circular room untouched by time. Bioluminescent "
                "moss paints the walls in soft blue-green light. On a stone "
                "altar sits a single ancient scroll, its seal unbroken for "
                "a thousand years. The air hums with quiet power."
            ),
            exits={"east": "library"},
            items=["ancient scroll"],
        ),
        "garden": Room(
            room_id="garden",
            name="Garden",
            description=(
                "A peaceful clearing hidden beneath the dungeon's ceiling. "
                "A single cherry tree blooms impossibly here, petals drifting "
                "down onto soft moss. A stone bench invites rest. Carved "
                "into the trunk: \"You've found peace.\" "
                "The only way back is NORTH."
            ),
            exits={"north": "entrance_hall"},
            items=[],
        ),
    }

    return rooms, items


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    rooms, items = build_dungeon()
    engine = GameEngine(rooms, items)
    engine.run()
