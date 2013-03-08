# coding: utf8
from __future__ import absolute_import
import re
import datetime
import dateutil.parser
from .dateutil import get_parserinfo
from .log import Log
import logging
from collections import namedtuple

log = logging.getLogger(__name__)

Line = namedtuple("Line", ("num", "ind", "mark", "line"))


class Regexps(object):
    """
    Repository of precompiled regexps
    """
    def __init__(self):
        self.event_range = re.compile(r"\s*--\s*")
        self.meta_head = re.compile(r"^\w.*:")
        self.log_date = re.compile("^(?:(?P<year>\d{4})|-+\s*(?P<date>.+?))\s*$")
        self.log_head = re.compile(r"^(?P<date>(?:\S| \d).*?):\s+(?P<start>\d+:\d+)-\s*(?P<end>\d+:\d+)?")


def annotate_with_indent_and_markers(lines, first_lineno=1):
    """
    Annotate each line with indent level and bullet marker

    Markers are:
        None: ordinary line
         ' ': empty line
         '-': dash bullet
         '*': star bullet
    """
    last_indent = 0
    last_empty_lines = []
    for lineno, l in enumerate(lines):
        if not l or l.isspace():
            # Empty line, get indent of previous line if followed by
            # continuation of same or higher indent level, else indent 0
            last_empty_lines.append((lineno, l))
        else:
            # Compute indent
            lev = 0
            mlev = 0
            marker = None
            for c in l:
                if c == ' ':
                    lev += 1
                elif c == '\t':
                    lev += 8
                elif marker is None and c in "*-":
                    marker = c
                    mlev = lev
                    lev += 1
                else:
                    break
            if last_empty_lines:
                if marker is None:
                    mlev = lev
                if mlev >= last_indent:
                    empty_lev = last_indent
                else:
                    empty_lev = 0
                for i, x in last_empty_lines:
                    yield Line(first_lineno + i, empty_lev, ' ', x)
                last_empty_lines = []
            last_indent = lev
            yield Line(first_lineno + lineno, lev, marker, l)
    for i, l in last_empty_lines:
        yield Line(first_lineno + i, 0, ' ', l)


class GeneratorLookahead(object):
    """
    Wrap a generator providing a 1-element lookahead
    """
    def __init__(self, gen):
        self.gen = gen
        self.has_lookahead = False
        self.lookahead = None

    def peek(self):
        if not self.has_lookahead:
            self.lookahead = self.gen.next()
            self.has_lookahead = True
        return self.lookahead

    def pop(self):
        if self.has_lookahead:
            self.has_lookahead = False
            return self.lookahead
        else:
            return self.gen.next()


class EventParser(object):
    def __init__(self, re=None, lang=None):
        self.re = Regexps() if re is None else re
        self.lang = lang
        # Defaults for missing parsedate values
        self.default = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.parserinfo = get_parserinfo(lang)
        # TODO: remember the last date to use as default for time-only things

    def parse(self, s, set_default=True):
        try:
            d = dateutil.parser.parse(s, default=self.default, parserinfo=self.parserinfo)
            if set_default:
                self.default = d.replace(hour=0, minute=0, second=0, microsecond=0)
            return d
        except ValueError:
            return None

    def _to_event(self, dt):
        if dt is None: return None
        return dict(
            start=dt,
            end=None,
            allDay=(dt.hour == 0 and dt.minute == 0 and dt.second == 0)
        )

    def parsedate(self, s):
        """
        Return the parsed date, or None if it wasn't recognised
        """
        if not s:
            return None
        mo = self.re.event_range.search(s)
        if mo:
            #print "R"
            # Parse range
            since = s[:mo.start()]
            until = s[mo.end():]
            since = self.parse(since)
            until = self.parse(until, set_default=False)
            return dict(
                start=since,
                end=until,
                allDay=False,
            )
        elif s[0].isdigit():
            #print "D"
            return self._to_event(self.parse(s))
        elif s.startswith("d:"):
            #print "P"
            return self._to_event(self.parse(s[2:]))
        return None


class Spacer(object):
    TAG = "spacer"

    def __init__(self, lines):
        self.lines = lines


class FreeformText(object):
    TAG = "freeform"

    def __init__(self, lines):
        self.lines = lines


class NextActions(object):
    TAG = "next-actions"

    def __init__(self, lines, contexts=None, event=None):
        # TODO: identify datetimes and parse them into datetime objects
        self.contexts = frozenset(contexts) if contexts is not None else frozenset()
        self.lines = lines
        self.event = event

    def at(self, ev):
        """
        Return a copy of this next action list, with a given event
        """
        return NextActions(list(self.lines), self.contexts, ev)

    def add_to_vobject(self, cal):
        if self.event is None: return
        vevent = cal.add("vevent")
        vevent.add("categories").value = list(self.contexts)
        vevent.add("dtstart").value = self.event["start"]
        if self.event["end"]:
            vevent.add("dtend").value = self.event["start"]
        if len(self.lines) > 1:
            vevent.add("summary").value = self.lines[1].strip(" -")
        vevent.add("description").value = "\n".join(self.lines[1:])


class SomedayMaybe(object):
    TAG = "someday-maybe"

    def __init__(self, lines):
        self.lines = lines


def parsetime(s):
    h, m = s.split(":")
    return datetime.time(int(h), int(m), 0)


class LogParser(object):
    def __init__(self, re=None, lang=None):
        self.re = Regexps() if re is None else re
        self.ep = EventParser(re=re, lang=lang)
        self.ep.default = datetime.datetime(datetime.date.today().year, 1, 1)
        self.begin = None
        self.until = None
        self.logbody = []

    def flush(self):
        res = Log(self.begin, self.until, "\n".join(self.logbody))
        self.begin = None
        self.end = None
        self.logbody = []
        return res

    def is_log(self, lines):
        """
        Check if the next line looks like the start of a log block
        """
        first = lines.peek()
        return self.re.log_date.match(first) or self.re.log_head.match(first)

    def parse(self, lines):
        entries = []
        while True:
            line = lines.next()
            if not line: break

            # Look for a date context
            mo = self.re.log_date.match(line)
            if mo:
                if self.begin is not None:
                    entries.append(self.flush())
                val = mo.group("date") or mo.group("year")
                log.debug("%s:%d: stand-alone date: %s", lines.fname, lines.lineno, val)
                # Just parse the next line, storing it nowhere, but updating
                # the 'default' datetime context
                self.ep.parse(val)
                continue

            # Look for a log head
            mo = self.re.log_head.match(line)
            if mo:
                try:
                    if self.begin is not None:
                        entries.append(self.flush())
                    log.debug("%s:%d: log header: %s %s-%s", lines.fname, lines.lineno, mo.group("date"), mo.group("start"), mo.group("end"))
                    date = self.ep.parse(mo.group("date"))
                    if date is None:
                        log.warning("%s:%d: cannot parse log header date: '%s' (lang=%s)", lines.fname, lines.lineno, mo.group("date"), self.ep.lang)
                        date = self.ep.default
                    date = date.date()
                    self.begin = datetime.datetime.combine(date, parsetime(mo.group("start")))
                    if mo.group("end"):
                        self.until = datetime.datetime.combine(date, parsetime(mo.group("end")))
                        if self.until < self.begin:
                            # Deal with intervals across midnight
                            self.until += datetime.timedelta(days=1)
                    else:
                        self.until = None
                    continue
                except ValueError, e:
                    log.error("%s:%d: %s", lines.fname, lines.lineno, str(e))

            # Else append to the previous log body
            self.logbody.append(line)

        if self.begin is not None:
            entries.append(self.flush())

        return entries


class BodyParser(object):
    def __init__(self, lines, re=None, lang=None, fname=None, first_lineno=1):
        self.re = Regexps() if re is None else re
        self.lang = lang
        self.fname = fname
        self.first_lineno = first_lineno
        # Annotated lines generator
        self.lines = GeneratorLookahead(annotate_with_indent_and_markers(lines, first_lineno))
        self.parsed = []

    def add_to_spacer(self, line):
        if not self.parsed or self.parsed[-1].TAG != Spacer.TAG:
            self.parsed.append(Spacer([]))
        self.parsed[-1].lines.append(line)

    def add_to_freeform(self, line):
        if not self.parsed or self.parsed[-1].TAG != FreeformText.TAG:
            self.parsed.append(FreeformText([]))
        self.parsed[-1].lines.append(line)

    def parse_body(self):
        try:
            self.parse_next_actions()
            self.parse_someday_maybe()
        except StopIteration:
            pass
        return self.parsed

    def parse_next_actions(self):
        eparser = EventParser(re=self.re, lang=self.lang)
        while True:
            lineno, i, m, l = self.lines.peek()

            if m == '*':
                log.debug("%s:%d: next action terminator '%s'", self.fname, lineno, l)
                # End of next actions, return the first line of someday/maybe
                return
            elif i == 0 and l.endswith(":"):
                #log.debug("%s:%d: next action context '%s'", self.lines.fname, self.lines.lineno, l)
                log.debug("%s:%d: next action context '%s'", self.fname, lineno, l)
                # Start of a context line
                contexts = []
                events = []
                for t in re.split(r"\s*,\s*", l.strip(" :\t")):
                    ev = eparser.parsedate(t)
                    if ev is not None:
                        events.append(ev)
                    else:
                        contexts.append(t)
                self.parse_next_action_list(contexts, events)
            elif m == '-':
                log.debug("%s:%d: contextless next action list '%s'", self.fname, lineno, l)
                # Contextless context lines
                self.parse_next_action_list()
            elif m == ' ':
                log.debug("%s:%d: spacer '%s'", self.fname, lineno, l)
                # Empty lines
                self.add_to_spacer(l)
                self.lines.pop()
            else:
                log.debug("%s:%d: freeform text '%s'", self.fname, lineno, l)
                # Freeform text
                self.add_to_freeform(l)
                self.lines.pop()

    def parse_next_action_list(self, contexts=None, events=[]):
        na = NextActions([], contexts)

        if contexts is not None:
            # Store the context line
            na.lines.append(self.lines.pop()[3])

        last_indent = None
        while True:
            try:
                lineno, i, m, l = self.lines.peek()
            except StopIteration:
                break
            if m == "*": break
            if last_indent is None:
                last_indent = i
            if i < last_indent:
                break
            na.lines.append(l)
            self.lines.pop()
            last_indent = i

        if not events:
            log.debug("%s:%d: add eventless next action", self.fname, lineno)
            self.parsed.append(na)
        else:
            for e in events:
                log.debug("%s:%d: add eventful next action start=%s", self.fname, lineno, e["start"])
                self.parsed.append(na.at(e))

    def parse_someday_maybe(self):
        log.debug("%s:%d: parsing someday/maybe", self.fname, self.lines.peek().num)
        self.parsed.append(SomedayMaybe([]))
        while True:
            lineno, i, m, l = self.lines.pop()
            self.parsed[-1].lines.append(l)


class ProjectParser(object):
    def __init__(self, re=None):
        self.re = Regexps() if re is None else re
        self.lines = None
        # Current line being parsed
        self.lineno = 0
        # Defaults
        self.meta = dict()

    def peek(self):
        """
        Return the next line to be parsed, without advancing the cursor.
        Return None if we are at the end.
        """
        if self.lineno < len(self.lines):
            return self.lines[self.lineno]
        else:
            return None

    def next(self):
        """
        Return the next line to be parsed, advancing the cursor.
        Return None if we are at the end.
        """
        if self.lineno < len(self.lines):
            res = self.lines[self.lineno]
            self.lineno += 1
            return res
        else:
            return None

    def discard(self):
        """
        Just advance the cursor to the next line
        """
        if self.lineno < len(self.lines):
            self.lineno += 1

    def skip_empty_lines(self):
        while True:
            l = self.peek()
            if l is None: break
            if l: break
            self.discard()

    def parse_meta(self):
        first = self.peek()

        self.firstline_meta = None
        self.meta = dict()

        # If it starts with a log, there is no metadata: stop
        if self.re.log_date.match(first) or self.re.log_head.match(first):
            return

        # If the first line doesn't look like a header, stop
        if not self.re.meta_head.match(first):
            return

        log.debug("%s:%d: parsing metadata", self.fname, self.lineno)
        self.firstline_meta = self.lineno

        # Get everything until we reach an empty line
        meta = []
        while True:
            l = self.next()
            # Stop at an empty line or at EOF
            if not l: break
            meta.append(l)

        # Parse like an email headers
        import email
        self.meta = dict(((k.lower(), v) for k, v in email.message_from_string("\n".join(meta)).items()))

    def parse_log(self):
        lp = LogParser(re=self.re, lang=self.meta.get("lang", None))
        if lp.is_log(self):
            log.debug("%s:%d: parsing log", self.fname, self.lineno)
            self.firstline_log = self.lineno
            self.log = lp.parse(self)
        else:
            self.firstline_log = None
            self.log = []

    def parse_body(self):
        log.debug("%s:%d: parsing body", self.fname, self.lineno)
        bp = BodyParser(self.lines[self.lineno:], re=self.re, lang=self.meta.get("lang", None), fname=self.fname, first_lineno=self.lineno)
        bp.parse_body()
        self.body = bp.parsed

    def parse(self, fname=None, fd=None):
        self.fname = fname

        # Read the file, split in trimmed lines
        if fd is None:
            with open(fname) as fd:
                self.lines = [x.rstrip() for x in fd]
        else:
            self.lines = [x.rstrip() for x in fd]

        # Reset current line cursor
        self.lineno = 0

        # Parse metadata
        self.parse_meta()

        self.skip_empty_lines()

        # Parse log entries
        self.parse_log()

        self.skip_empty_lines()

        # Parse/store body
        self.parse_body()
