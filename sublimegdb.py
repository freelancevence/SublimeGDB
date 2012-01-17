"""
Copyright (c) 2012 Fredrik Ehnbom

This software is provided 'as-is', without any express or implied
warranty. In no event will the authors be held liable for any damages
arising from the use of this software.

Permission is granted to anyone to use this software for any purpose,
including commercial applications, and to alter it and redistribute it
freely, subject to the following restrictions:

   1. The origin of this software must not be misrepresented; you must not
   claim that you wrote the original software. If you use this software
   in a product, an acknowledgment in the product documentation would be
   appreciated but is not required.

   2. Altered source versions must be plainly marked as such, and must not be
   misrepresented as being the original software.

   3. This notice may not be removed or altered from any source
   distribution.
"""
import sublime
import sublime_plugin
import subprocess
import threading
import time
import traceback
import os
import re
import Queue


breakpoints = {}
gdb_lastresult = ""
gdb_lastline = ""
gdb_cursor = ""
gdb_cursor_position = 0

gdb_process = None
gdb_session_view = None
gdb_console_view = None
gdb_locals_view = None
result_regex = re.compile("(?<=\^)[^,]*")


class GDBView:
    LINE = 0
    FOLD_ALL = 1

    def __init__(self, name):
        self.queue = Queue.Queue()
        self.name = name
        self.create_view()

    def add_line(self, line):
        self.queue.put((GDBView.LINE, line))
        sublime.set_timeout(self.update, 0)

    def create_view(self):
        self.view = sublime.active_window().new_file()
        self.view.set_name(self.name)
        self.view.set_scratch(True)
        self.view.set_read_only(True)

    def fold_all(self):
        self.queue.put((GDBView.FOLD_ALL, None))

    def update(self):
        e = self.view.begin_edit()
        self.view.set_read_only(False)
        try:
            while True:
                cmd, data = self.queue.get_nowait()
                if cmd == GDBView.LINE:
                    self.view.insert(e, self.view.size(), data)
                elif cmd == GDBView.FOLD_ALL:
                    self.view.run_command("fold_all")

                self.queue.task_done()
        except:
            pass
        finally:
            self.view.end_edit(e)
            self.view.set_read_only(True)
            self.view.show(self.view.size())


class GDBValuePairs:
    def __init__(self, string):
        string = string.split(",")
        self.data = {}
        for pair in string:
            print "pair: %s" % pair
            if not "=" in pair:
                continue
            key, value = pair.split("=")
            value = value.replace("\"", "")
            print key, value
            self.data[key] = value

    def __getitem__(self, key):
        return self.data[key]

    def __str__(self):
        return "%s" % self.data


def variable_stuff(line, indent=""):
    line = line[line.find("value=") + 7:]
    if line[0] == "{":
        line = line[1:]
    start = 0
    level = 0
    output = ""
    for idx in range(len(line)):
        char = line[idx]
        if char == '{':
            data = line[start:idx].strip()
            output += "%s%s\n" % (indent, data)

            start = idx + 1
            indent = indent + "\t"
        elif char == '}':
            output += "%s%s" % (indent, line[start:idx].strip())
            start = idx + 1
            indent = indent[:-1]
        elif char == "," and level == 0:
            data = line[start:idx].strip()
            output += "%s%s\n" % (indent, data)
            start = idx + 1
        elif char == "\"":
            data = line[start:idx].strip()
            output += "%s%s\n" % (indent, data)
            break
        elif char == "(" or char == "<":
            level += 1
        elif char == ")" or char == ">":
            level -= 1

    return output


def locals(line):
    varobjs = line[:line.rfind("}}") + 1]
    varobjs = varobjs.split("varobj=")[1:]

    for varobj in varobjs:
        var = GDBValuePairs(varobj[1:-1])
        gdb_locals_view.add_line("%s %s %s=(%s) %s\n" % (var["typecode"], var["type"], var["exp"], var["dynamic_type"], var["value"]))
        try:
            data = run_cmd("-data-evaluate-expression %s" % var["exp"], True)
            gdb_locals_view.add_line(variable_stuff(data, "\t"))
        except:
            traceback.print_exc()

    gdb_locals_view.fold_all()


def extract_breakpoints(line):
    gdb_breakpoints = []
    bps = re.findall("(?<=,bkpt\=\{)[^}]+", line)
    for bp in bps:
        gdb_breakpoints.append(GDBValuePairs(bp))
    return gdb_breakpoints


def extract_stackframes(line):
    gdb_stackframes = []
    frames = re.findall("(?<=frame\=\{)[^}]+", line)
    for frame in frames:
        gdb_stackframes.append(GDBValuePairs(frame))
    return gdb_stackframes


def update(view=None):
    if view == None:
        view = sublime.active_window().active_view()
    bps = []
    fn = view.file_name()
    if fn in breakpoints:
        for line in breakpoints[fn]:
            if not (line == gdb_cursor_position and fn == gdb_cursor):
                bps.append(view.full_line(view.text_point(line - 1, 0)))
    view.add_regions("sublimegdb.breakpoints", bps, "keyword.gdb", "circle", sublime.HIDDEN)
    cursor = []

    if fn == gdb_cursor and gdb_cursor_position != 0:
        cursor.append(view.full_line(view.text_point(gdb_cursor_position - 1, 0)))

    view.add_regions("sublimegdb.position", cursor, "entity.name.class", "bookmark", sublime.HIDDEN)

count = 0


def run_cmd(cmd, block=False):
    global count
    count = count + 1
    cmd = "%d%s\n" % (count, cmd)
    gdb_session_view.add_line(cmd)
    gdb_process.stdin.write(cmd)
    if block:
        countstr = "%d^" % count
        while not gdb_lastresult.startswith(countstr):
            time.sleep(0.1)
        return gdb_lastresult
    return count


def wait_until_stopped():
    result = run_cmd("-exec-interrupt", True)
    if "^done" in result:
        while not "stopped" in gdb_lastline:
            time.sleep(0.1)
        return True
    return False


def resume():
    run_cmd("-exec-continue")


def add_breakpoint(filename, line):
    breakpoints[filename].append(line)
    if is_running():
        res = wait_until_stopped()
        run_cmd("-break-insert %s:%d" % (filename, line))
        if res:
            resume()


def remove_breakpoint(filename, line):
    breakpoints[filename].remove(line)
    if is_running():
        res = wait_until_stopped()
        gdb_breakpoints = extract_breakpoints(run_cmd("-break-list", True))
        for bp in gdb_breakpoints:
            if bp.data["file"] == filename and bp.data["line"] == str(line):
                run_cmd("-break-delete %s" % bp.data["number"])
                break
        if res:
            resume()


def toggle_breakpoint(filename, line):
    if line in breakpoints[filename]:
        remove_breakpoint(filename, line)
    else:
        add_breakpoint(filename, line)


def sync_breakpoints():
    global breakpoints
    newbps = {}
    for file in breakpoints:
        for bp in breakpoints[file]:
            cmd = "-break-insert %s:%d" % (file, bp)
            out = run_cmd(cmd, True)
            bp = extract_breakpoints(out)[0]
            f = bp["file"]
            if not f in newbps:
                newbps[f] = []
            newbps[f].append(int(bp["line"]))
    breakpoints = newbps
    update()


def get_result(line):
    return result_regex.search(line).group(0)


def update_cursor():
    global gdb_cursor
    global gdb_cursor_position
    line = run_cmd("-stack-info-frame", True)
    if get_result(line) == "error":
        gdb_cursor_position = 0
        update()
        return
    frames = extract_stackframes(line)
    print line
    print "%s" % frames[0]
    gdb_cursor = frames[0]["fullname"]
    gdb_cursor_position = int(frames[0]["line"])
    sublime.active_window().open_file("%s:%d" % (gdb_cursor, gdb_cursor_position), sublime.ENCODED_POSITION)
    update()
    locals(run_cmd("-stack-list-locals 2", True))


def gdboutput(pipe):
    global gdb_process
    global gdb_lastresult
    global gdb_lastline
    command_result_regex = re.compile("^\d+\^")
    stopped_regex = re.compile("^\d*\*stopped")
    while True:
        try:
            if gdb_process.poll() != None:
                break
            line = pipe.readline().strip()

            if len(line) > 0:
                gdb_session_view.add_line("%s\n" % line)

                if stopped_regex.match(line) != None:
                    sublime.set_timeout(update_cursor, 0)
                if not line.startswith("(gdb)"):
                    gdb_lastline = line
                if "BreakpointTable" in line:
                    extract_breakpoints(line)
                if command_result_regex.match(line) != None:
                    gdb_lastresult = line

                if line.startswith("~"):
                    gdb_console_view.add_line(
                        line[2:-1].replace("\\n", "\n").replace("\\\"", "\"").replace("\\t", "\t"))

        except:
            traceback.print_exc()
    if pipe == gdb_process.stdout:
        gdb_session_view.add_line("GDB session ended\n")
    global gdb_cursor_position
    gdb_cursor_position = 0
    sublime.set_timeout(update, 0)


def show_input():
    sublime.active_window().show_input_panel("GDB", "", input_on_done, input_on_change, input_on_cancel)


def input_on_done(s):
    run_cmd(s)
    if s.strip() != "quit":
        show_input()


def input_on_cancel():
    pass


def input_on_change(s):
    pass


def get_setting(key, default=None):
    try:
        s = sublime.active_window().active_view().settings()
        if s.has("sublimegdb_%s" % key):
            return s.get("sublimegdb_%s" % key)
    except:
        pass
    return sublime.load_settings("SublimeGDB.sublime-settings").get(key, default)


def is_running():
    return gdb_process != None and gdb_process.poll() == None


class GdbInput(sublime_plugin.TextCommand):
    def run(self, edit):
        show_input()


class GdbLaunch(sublime_plugin.TextCommand):
    def run(self, edit):
        global gdb_process
        global gdb_session_view
        global gdb_console_view
        global gdb_locals_view
        if gdb_process == None or gdb_process.poll() != None:
            os.chdir(get_setting("workingdir", "/tmp"))
            commandline = get_setting("commandline")
            commandline.insert(1, "--interpreter=mi")
            gdb_process = subprocess.Popen(commandline, shell=False, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            gdb_session_view = GDBView("GDB Session")
            gdb_console_view = GDBView("GDB Console")
            gdb_locals_view = GDBView("GDB locals")
            t = threading.Thread(target=gdboutput, args=(gdb_process.stdout,))
            t.start()

            sync_breakpoints()
            gdb_process.stdin.write("-exec-run\n")
            show_input()
        else:
            sublime.status_message("GDB is already running!")


class GdbContinue(sublime_plugin.TextCommand):
    def run(self, edit):
        global gdb_cursor_position
        gdb_cursor_position = 0
        update(self.view)
        run_cmd("-exec-continue")


class GdbExit(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-gdb-exit")


class GdbPause(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-gdb-interrupt")


class GdbStepOver(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-exec-next")


class GdbStepInto(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-exec-step")


class GdbNextInstruction(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-exec-next-instruction")


class GdbStepOut(sublime_plugin.TextCommand):
    def run(self, edit):
        run_cmd("-exec-finish")


class GdbToggleBreakpoint(sublime_plugin.TextCommand):
    def run(self, edit):
        fn = self.view.file_name()
        if fn not in breakpoints:
            breakpoints[fn] = []

        line, col = self.view.rowcol(self.view.sel()[0].a)
        toggle_breakpoint(fn, line + 1)
        update(self.view)


class GdbEventListener(sublime_plugin.EventListener):
    def on_query_context(self, view, key, operator, operand, match_all):
        global gdb_process
        if key != "gdb_running":
            return None
        return is_running() == operand

    def on_activated(self, view):
        if view.file_name() != None:
            update(view)

    def on_load(self, view):
        if view.file_name() != None:
            update(view)
