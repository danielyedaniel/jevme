"""The tool catalog: what Jevme can do, how Jev is asked about it, and how each runs.

A Tool has:
  - name, description (`what`), examples: fed to Jev as the intent Choice criteria
  - enum args: each becomes a speculative Choice in the same Jev request
  - an optional free-text arg: filled by span selection over the tail of the utterance
  - run(args) -> short human label for the overlay
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import actions as A


@dataclass
class EnumArg:
    name: str
    question: str
    criteria: dict[str, Any] | Callable[[], dict[str, Any]]

    def options(self) -> dict[str, Any]:
        return self.criteria() if callable(self.criteria) else self.criteria


@dataclass
class TextArg:
    name: str
    question: str
    optional: bool = False


@dataclass
class Tool:
    name: str
    what: str
    examples: list[str]
    run: Callable[[dict[str, str]], str]
    enum_args: list[EnumArg] = field(default_factory=list)
    text_arg: TextArg | None = None
    not_for: str | None = None
    instant: bool = True  # fire as soon as the decision is stable (no free text to wait for)

    def criteria(self) -> dict[str, Any]:
        c: dict[str, Any] = {"what": self.what, "examples": self.examples}
        if self.not_for:
            c["not_for"] = self.not_for
        return c


def _no_app(name: str) -> str:
    raise RuntimeError(f"no app called {name}")


def _see():
    from . import see
    return see


def _app_options() -> dict[str, Any]:
    running = set(A.running_apps())
    names = dict.fromkeys(list(running) + A.installed_apps())
    return {name: ("running" if name in running else None) for name in names}


def _running_options() -> dict[str, Any]:
    return {name: None for name in A.running_apps()} or {"__none__": "nothing to quit"}


TOOLS: list[Tool] = [
    Tool("open_app", "Open, launch, or switch to an application and bring it to the front.",
         ["open up the notes app", "open Chrome", "switch to Spotify", "go back to the browser"],
         run=lambda a: f"Opened {A.open_app(a['app']) or _no_app(a['app'])}",
         enum_args=[EnumArg("app", "Which application?", _app_options)],
         not_for="Websites or URLs (open_site), web searches (web_search)."),
    Tool("quit_app", "Quit a running application.",
         ["quit Spotify", "close Chrome", "exit the notes app"],
         run=lambda a: f"Quit {A.quit_app(a['app'])}",
         enum_args=[EnumArg("app", "Which running application should quit?", _running_options)],
         not_for="Closing a single tab or window (close_tab, minimize_window)."),
    Tool("web_search", "Google search for something in the browser.",
         ["google search Norbert Wiener", "search for the weather in Paris", "look up how tall the Eiffel tower is"],
         run=lambda a: f"Searched {a['query']}",
         text_arg=TextArg("query", "The exact search phrase to type into Google."), instant=False,
         not_for="Opening a named website or URL (open_site)."),
    Tool("open_site", "Open a website or URL in the browser: a well-known site name or a domain.",
         ["open up x.com", "go to youtube", "open gmail", "pull up github dot com"],
         run=lambda a: f"Opened {A.open_site(a['site'])}",
         text_arg=TextArg("site", "The site name or URL to open, e.g. 'x.com', 'youtube'."), instant=False,
         not_for="Searching the web for a phrase (web_search)."),
    Tool("site_search", "Search for something on a specific well-known site (Amazon, YouTube, eBay, Reddit, "
         "GitHub, Wikipedia, Google Maps...).",
         ["search for a rice cooker on amazon", "look up airpods on ebay", "search reddit for mechanical keyboards",
          "find coffee shops on google maps", "search youtube for lofi beats"],
         run=lambda a: A.site_search(a["site"], a["query"]),
         text_arg=TextArg("query", "The thing to search for, without the site name."),
         enum_args=[EnumArg("site", "Which site?", {k: None for k in A.SITE_SEARCH})], instant=False,
         not_for="A plain Google search (web_search); playing a specific YouTube video (youtube_play); "
                 "searching inside whatever page is already open (general)."),
    Tool("youtube_play", "Find a video on YouTube by name and start playing it (first result).",
         ["play the love hypothesis trailer", "play some lo-fi beats on youtube", "put on the new Dune trailer",
          "play Bohemian Rhapsody on youtube"],
         run=lambda a: A.youtube_play(a["query"]),
         text_arg=TextArg("query", "What video to search for on YouTube: the title, song, or topic."), instant=False,
         not_for="Opening youtube.com without a specific video (open_site); clicking a video already on screen (click_thing)."),
    Tool("click_thing", "Click something visible on screen in any app: a button, link, video, result, menu, "
         "tab, field, or icon, described by its label or position.",
         ["click the second video", "open the first result", "press the subscribe button", "click on shorts",
          "click the search box", "hit the blue send button", "click cancel", "right click the first result",
          "double click the folder called photos"],
         run=lambda a: _see().click(a["target"], a.get("how", "click")),
         text_arg=TextArg("target", "What to click, exactly as the user described it (e.g. 'the second video', "
                          "'the subscribe button', 'search box')."),
         enum_args=[EnumArg("how", "How to click it?", {"click": "a normal click (default)",
                                                        "double": "double-click", "right": "right-click / context menu"})],
         instant=False,
         not_for="Playing a video by name (youtube_play); opening a site (open_site); opening an app (open_app)."),
    Tool("type_into", "Click a described field or box on screen and then type the dictated text into it.",
         ["in the search box type cats", "type hello in the message field", "put my name in the name box"],
         run=lambda a: _see().type_into({"search": "the search box", "message": "the message or chat box",
                                         "address": "the address bar", "other": "the text field"}.get(a.get("target", ""), ""),
                                        a["text"]),
         text_arg=TextArg("text", "The exact words to type, without the field name."),
         enum_args=[EnumArg("target", "Which field?", {"search": "a search box", "message": "a message / chat box",
                                                       "address": "the browser address bar", "focused": "wherever the cursor is",
                                                       "other": "some other named field"})],
         instant=False,
         not_for="Typing where the cursor already is (type_text)."),
    Tool("notes_new_note", "Create a new note in Apple Notes, optionally with a title / first line.",
         ["create a new note", "make a new note and make the title say hello", "new note called groceries"],
         run=lambda a: f"New note{': ' + a['title'] if a.get('title') else ''}" if not A.notes_new_note(a.get("title")) else "",
         text_arg=TextArg("title", "The title or first line of the note, if the user dictated one.", optional=True),
         instant=False),
    Tool("type_text", "Type dictated text into whatever is focused right now.",
         ["type hello world", "write out see you at three", "type in Alex"],
         run=lambda a: f"Typed “{a['text']}”" if not A.type_text(a["text"]) else "",
         text_arg=TextArg("text", "The exact words to type, nothing else."), instant=False,
         not_for="Search queries (web_search) or note titles (notes_new_note)."),
    Tool("photo_booth_take_picture", "Open Photo Booth and take a picture with the camera.",
         ["take a picture of me", "open photo booth and take a photo", "snap a selfie"],
         run=lambda a: "Took a picture" if not A.photo_booth_take_picture() else "",
         not_for="Screenshots of the screen (take_screenshot)."),
    Tool("take_screenshot", "Capture the whole screen to a PNG on the Desktop.",
         ["take a screenshot", "grab the screen"],
         run=lambda a: "Screenshot saved" if not A.screenshot() else ""),
    Tool("set_volume", "Change the Mac output volume.",
         ["mute", "turn it down", "make it louder", "volume to max", "unmute"],
         run=lambda a: f"Volume {a['level']}" if not A.set_volume(a["level"]) else "",
         enum_args=[EnumArg("level", "How should the volume change?",
                            {"mute": "silence output", "unmute": "restore output", "louder": "a bit louder",
                             "quieter": "a bit quieter", "max": "maximum"})]),
    Tool("media_play_pause", "Play or pause the music (Spotify or Apple Music).",
         ["pause", "play", "pause the music", "resume spotify", "stop the music"],
         run=lambda a: f"{A.media('play_pause')} play/pause",
         not_for="Playing a specific song, artist, album, playlist or video by name ('play Drake', 'play lofi "
                 "beats'): that has to be found first, so it is not a bare play/pause."),
    Tool("media_next", "Skip to the next track.", ["next song", "skip this"],
         run=lambda a: f"{A.media('next')} next"),
    Tool("media_previous", "Go back to the previous track.", ["previous song", "go back a track"],
         run=lambda a: f"{A.media('previous')} previous"),
    Tool("new_tab", "Open a new empty tab in the front browser.", ["new tab", "open another tab"],
         run=lambda a: "New tab" if not A.keystroke("t", "cmd") else "",
         not_for="Opening a specific site (open_site), searching (web_search), or switching to an EXISTING "
                 "tab by position or name like 'the second tab' (that is a click, not a new tab)."),
    Tool("close_tab", "Close the current browser tab or window.", ["close this tab", "close that"],
         run=lambda a: "Closed tab" if not A.keystroke("w", "cmd") else ""),
    Tool("reload_page", "Reload the current page.", ["refresh", "reload the page"],
         run=lambda a: "Reloaded" if not A.keystroke("r", "cmd") else ""),
    Tool("go_back", "Go back to the previous page in the browser.", ["go back", "back a page"],
         run=lambda a: "Back" if not A.keystroke("[", "cmd") else "",
         not_for="Switching back to an application (open_app)."),
    Tool("scroll", "Scroll the front window up or down.", ["scroll down", "scroll up a bit"],
         run=lambda a: f"Scrolled {a['direction']}" if not A.scroll(a["direction"]) else "",
         enum_args=[EnumArg("direction", "Which direction?", {"down": None, "up": None})]),
    Tool("press_key", "Press a single key: enter, escape, tab, space, delete.",
         ["hit enter", "press escape", "submit that"],
         run=lambda a: f"Pressed {a['key']}" if not A.keystroke(a["key"]) else "",
         enum_args=[EnumArg("key", "Which key?", {"enter": "return/submit", "escape": None, "tab": None,
                                                  "space": None, "delete": "backspace"})]),
    Tool("select_all", "Select all in the focused app.", ["select everything", "select all"],
         run=lambda a: "Selected all" if not A.keystroke("a", "cmd") else ""),
    Tool("copy", "Copy the selection.", ["copy that"], run=lambda a: "Copied" if not A.keystroke("c", "cmd") else ""),
    Tool("paste", "Paste the clipboard.", ["paste it here"], run=lambda a: "Pasted" if not A.keystroke("v", "cmd") else ""),
    Tool("undo", "Undo the last edit.", ["undo that"], run=lambda a: "Undid" if not A.keystroke("z", "cmd") else ""),
    Tool("save", "Save the current document.", ["save this"], run=lambda a: "Saved" if not A.keystroke("s", "cmd") else ""),
    Tool("minimize_window", "Minimize the front window.", ["minimize this"],
         run=lambda a: "Minimized" if not A.keystroke("m", "cmd") else ""),
    Tool("fullscreen_window", "Toggle full screen for the front window.", ["make it full screen", "full screen chrome"],
         run=lambda a: "Full screen" if not A.keystroke("f", "ctrl", "cmd") else ""),
    Tool("hide_others", "Hide every app except the one in front.", ["hide everything else", "clear the desktop"],
         run=lambda a: "Hid others" if not A.keystroke("h", "cmd", "alt") else ""),
    Tool("toggle_dark_mode", "Toggle macOS dark mode.", ["turn on dark mode", "switch to light mode"],
         run=lambda a: "Toggled dark mode" if not A.toggle_dark_mode() else ""),
    Tool("open_folder", "Open a common folder in Finder.", ["open my downloads", "show the desktop folder"],
         run=lambda a: f"Opened {A.open_folder(a['folder'])}",
         enum_args=[EnumArg("folder", "Which folder?", {"downloads": None, "desktop": None, "documents": None,
                                                        "home": "the user's home folder", "applications": None,
                                                        "pictures": None})]),
    Tool("send_draft", "Send or submit what is already typed or drafted in the front app (a message, reply, "
         "email, comment or form): press its Send button, or Return.",
         ["send", "send it", "send the message", "hit send", "submit", "send that", "press send"],
         run=lambda a: _see().send_draft(),
         not_for="Writing a new message to someone (general): that needs a recipient and content first."),
    Tool("read_screen", "Read out what is on screen: the visible text, headings and buttons of the front window.",
         ["what's on my screen", "what does this say", "read this page", "what am I looking at"],
         run=lambda a: _see().read()),
    Tool("lock_screen", "Lock the screen.", ["lock my screen", "lock the mac"],
         run=lambda a: "Locked" if not A.keystroke("q", "ctrl", "cmd") else ""),
]

BY_NAME = {t.name: t for t in TOOLS}

# Non-action outcomes that always sit beside the tools in the intent Choice.
NOT_YET = "not_yet"
CHAT = "chat"
UNSUPPORTED = "unsupported"
GENERAL = "general"
CANCEL = "cancel"

GENERAL_CRITERIA = {
    "what": "Any other complete instruction that is carried out by using the apps and windows on screen: "
            "multi-step tasks, anything about what is currently on screen, filling forms, sending messages, "
            "navigating inside an app, or a request that combines several steps.",
    "examples": ["reply to this email saying I'll be there at 3", "open the third tab and scroll to the comments",
                 "in slack message alex that the demo moved", "add milk to my reminders list", "close the last four tabs",
                 "find the settings page in this app", "sort these files by date"],
    "not_for": "A single action a listed tool already does exactly (use that tool); chit-chat; unfinished sentences.",
}
CANCEL_CRITERIA = {
    "what": "The user wants the assistant to stop what it is doing right now.",
    "examples": ["stop", "cancel that", "no stop", "abort", "never mind stop"],
}

INTENT_INSTRUCTIONS = (
    "`utterance` is a LIVE, PARTIAL transcript of someone talking to a Mac voice assistant; it may be "
    "cut off mid-sentence and may contain filler ('okay', 'um', 'can you', 'once you're there'). "
    "`front_app` is focused, its window is titled `front_window` (for a browser, the page that is open), "
    "and `running_apps` are open. `recent_action` is what the assistant did a "
    "moment ago, already finished. Which single action is the utterance asking for NOW? Pick a tool only "
    "when the words already name a complete, specific action; prefer the tool scoped to what was said, "
    "not to what might come next. Choose `not_yet` while the request is still forming or only refers back "
    "to `recent_action`. Choose `chat` for thanks, reactions, or talk that is not an instruction. Choose "
    "`general` when the request is a clear, finished instruction that no listed tool does exactly but that "
    "can be done by operating apps on screen. Choose `unsupported` only for things no app window can do "
    "(system internals, files, settings)."
)

NOT_YET_CRITERIA = {
    "what": "No complete action yet: still mid-sentence, only filler or connective words, or words that just "
            "acknowledge or refer back to `recent_action` which is already done.",
    "examples": ["okay so", "and once you're there can you", "for me and", "great great", "now can you"],
    "not_for": "One-word commands that are complete on their own: 'pause', 'play', 'mute', 'copy', 'paste', "
               "'undo', 'back', 'refresh', 'skip', 'enter', 'stop'. Those are the matching tool.",
}
UNSUPPORTED_CRITERIA = {
    "what": "A clear, complete instruction that is not done through any app's windows: system settings, "
            "files on disk, hardware, scripts (a small program must be written for it).",
    "examples": ["change my wallpaper to a solid black", "make the screen brighter", "empty the trash",
                 "rename every file on my desktop to lowercase"],
}
CHAT_CRITERIA = {
    "what": "Reactions, thanks, or thinking aloud that ask for nothing.",
    "examples": ["nice nice", "cool awesome thank you", "hmm let me think"],
}
