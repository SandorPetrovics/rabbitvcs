#
# This is an extension to the Nautilus file manager to allow better 
# integration with the Subversion source control system.
# 
# Copyright (C) 2006-2008 by Jason Field <jason@jasonfield.com>
# Copyright (C) 2007-2008 by Bruce van der Kooij <brucevdkooij@gmail.com>
# Copyright (C) 2008-2008 by Adam Plumb <adamplumb@gmail.com>
# 
# RabbitVCS is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
# 
# RabbitVCS is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with RabbitVCS;  If not, see <http://www.gnu.org/licenses/>.
#

"""

Our module for everything related to the Thunar extension.
  
"""

from __future__ import with_statement
import copy
import os.path
from os.path import isdir, isfile, realpath, basename
import datetime
import time
import threading

import thunarx
import pysvn
import gobject
import gtk

from rabbitvcs.lib.vcs.svn import SVN

from rabbitvcs.util.vcs import *
from rabbitvcs.ui import STATUS_EMBLEMS
from rabbitvcs.lib.helper import launch_ui_window, launch_diff_tool
from rabbitvcs.lib.helper import get_file_extension, get_common_directory
from rabbitvcs.lib.helper import pretty_timedelta
from rabbitvcs.lib.decorators import timeit, disable
from rabbitvcs.lib.contextmenu import MainContextMenu, SEPARATOR

from rabbitvcs.lib.log import Log, reload_log_settings
log = Log("rabbitvcs.lib.extensions.thunarx.RabbitVCS")

from rabbitvcs import gettext
_ = gettext.gettext

from rabbitvcs.lib.settings import SettingsManager
settings = SettingsManager()

import rabbitvcs.services.service
from rabbitvcs.services.checkerservice import StatusCheckerStub as StatusChecker

class RabbitVCS(thunarx.MenuProvider):
    """ 
    This is the main class that implements all of our awesome features.
    
    """
    
    #: Maps statuses to emblems.
    #: TODO: should probably be possible to create this dynamically
    EMBLEMS = STATUS_EMBLEMS
    
    #: A list of statuses which count as modified (for a directory) in 
    #: TortoiseSVN emblem speak.
    MODIFIED_STATUSES = [
        SVN.STATUS["added"],
        SVN.STATUS["deleted"],
        SVN.STATUS["replaced"],
        SVN.STATUS["modified"],
        SVN.STATUS["missing"]
    ]
    
    MODIFIED_TEXT_STATUSES = [
        "added", 
        "deleted",
        "replaced",
        "modified",
        "missing"
    ]
    
    #: This is our lookup table for C{NautilusVFSFile}s which we need for attaching
    #: emblems. This is mostly a workaround for not being able to turn a path/uri
    #: into a C{NautilusVFSFile}. It looks like:::
    #: 
    #:     nautilusVFSFile_table = {
    #:        "/foo/bar/baz": <NautilusVFSFile>
    #:     
    #:     }
    #: 
    #: Keeping track of C{NautilusVFSFile}s is a little bit complicated because
    #: when an item is moved (renamed) C{update_file_info} doesn't get called. So
    #: we also add C{NautilusVFSFile}s to this table from C{get_file_items} etc.
    nautilusVFSFile_table = {}
    
    #: Without an actual status monitor it's not possible to just keep
    #: track of stuff that happens (e.g. a commit happens, files are added,
    #: such things). So at the moment we just add all interesting items
    #: to this list.
    monitored_files = []
    
    #: This is in case we want to permanently enable invalidation of the status
    #: checker info. We put a path here before we invalidate the item, so that
    #: we don't enter an endless loop when updating the status.
    #: The callback should acquire this lock when pushing the path to this.
    always_invalidate = True
    
    #: When we get the statuses from the callback, but them here for further
    #: use. There is a possible memory problem here if we put a lot of data in
    #: this - even when it's removed, Python may not release the memory. I do
    #: not know this for sure.
    #: This is of the form: [("path/to", {...status dict...}), ...]
    paths_from_callback = []
    
    paths_last_lookup = {}
    paths_lookup_timeout = 30
    
    #: It appears that the "update_file_info" call that is triggered by the
    #: "invalidate_extension_info" in the callback function happens
    #: synchronously (ie. in the same thread). However, given the nature of the
    #: python/nautilus extensions module, I'm not sure how reliable this is.
    #: It's certainly supported by debugging statements, but maybe it will
    #: change in the future? Who knows. This should work for both the current
    #: situation, and the possibility that they are asynchronous.
    callback_paths_lock = threading.RLock()
    
    #: A list of statuses that we want to keep track of for when a process
    #: might have done something.
    STATUSES_TO_MONITOR = copy.copy(MODIFIED_TEXT_STATUSES)
    STATUSES_TO_MONITOR.extend([
        "unversioned",
        # When doing a checkout Nautilus will notice a directory being
        # added and call update_file_info, but at that stage the
        # checkout likely hasn't completed yet and the status will be:
        "incomplete"
    ])
    
    def __init__(self):
        threading.currentThread().setName("RabbitVCS extension thread")
        
        # Create a global client we can use to do VCS related stuff
        self.vcs_client = SVN()
        
        self.status_checker = StatusChecker(None)
    
    def get_local_path(self, item):
        return item.get_uri().replace("file://", "")

    #~ @disable
    # @timeit
    def get_file_actions(self, window, items):
        """
        Menu activated with items selected. Nautilus also calls this function
        when rendering submenus, even though this is not needed since the entire
        menu has already been returned.
        
        Note that calling C{nautilusVFSFile.invalidate_extension_info()} will 
        also cause get_file_items to be called.
        
        @type   window: NautilusNavigationWindow
        @param  window:
        
        @type   items:  list of NautilusVFSFile
        @param  items:
        
        @rtype:         list of MenuItems
        @return:        The context menu entries to add to the menu.
        
        """

        paths = []
        for item in items:
            if self.valid_uri(item.get_uri()):
                path = realpath(unicode(self.get_local_path(item), "utf-8"))
                paths.append(path)
                self.nautilusVFSFile_table[path] = item

        if len(paths) == 0: return []
        
        return ThunarxMainContextMenu(self, window.get_data("base_dir"), paths).get_menu()
    
    #~ @disable
    @timeit
    def get_folder_actions(self, window, item):
        """
        Menu activated on entering a directory. Builds context menu for File
        menu and for window background.
        
        @type   window: NautilusNavigationWindow
        @param  window:
        
        @type   item:   NautilusVFSFile
        @param  item:
        
        @rtype:         list of MenuItems
        @return:        The context menu entries to add to the menu.
        
        """
        
        if not self.valid_uri(item.get_uri()): return
        path = realpath(unicode(self.get_local_path(item), "utf-8"))
        self.nautilusVFSFile_table[path] = item
        
        # log.debug("get_background_items() called")
        
        window.set_data("base_dir", path)
        
        return ThunarxMainContextMenu(self, path, [path]).get_menu()
            
    #
    # Helper functions
    #
    
    def valid_uri(self, uri):
        """
        Check whether or not it's a good idea to have RabbitVCS do
        its magic for this URI. Some examples of URI schemes:
        
        x-nautilus-desktop:/// # e.g. mounted devices on the desktop
        
        """
        
        if not uri.startswith("file://"): return False
        
        return True
    
    #
    # Some methods to help with keeping emblems up-to-date
    #
    
    def rescan_after_process_exit(self, proc, paths):
        """ 
        Rescans all of the items on our C{monitored_files} list after the
        process specified by C{proc} completes. Also checks the paths
        that were passed.
        
        TODO: the monitored_files list could grow quite large if somebody
        browses a lot of working copies. It probably won't affect anything
        (most importantly performance) all that negatively.
        
        """
        
        def do_check():
            # We'll check the paths first (these were the paths that
            # were originally passed along to the context menu). 
            #
            # This is needed among other things for:
            #
            #   - When a directory is normal and you add files inside it
            #
            for path in paths:
                # We're not interested in the result now, just the callback
                self.status_checker.check_status(path,
                                                 recurse=True,
                                                 invalidate=True,
                                                 callback=True,
                                                 summary=True)
            
        self.execute_after_process_exit(proc, do_check)
        
    def execute_after_process_exit(self, proc, func=None):

        def is_process_still_alive():
            log.debug("is_process_still_alive() for pid: %i" % proc.pid)
            # First we need to see if the commit process is still running

            retval = proc.poll()
            
            log.debug("%s" % retval)
            
            still_going = (retval is None)

            if not still_going and callable(func):
                func()
            
            return still_going

        # Add our callback function on a 1 second timeout
        gobject.timeout_add_seconds(1, is_process_still_alive)
        
    # 
    # Some other methods
    # 
    
    def reload_settings(self, proc):
        """
        Used to re-load settings after the settings dialog has been closed.
        
        FIXME: This probably doesn't belong here, ideally the settings manager
        does this itself and make sure everything is reloaded properly 
        after the settings dialogs saves.
        """
    
        def do_reload_settings():
            globals()["settings"] = SettingsManager()
            globals()["log"] = reload_log_settings()("rabbitvcs.lib.extensions.thunar")
            log.debug("Re-scanning settings")
            
        self.execute_after_process_exit(proc, do_reload_settings)


class RabbitVCSAction(gtk.Action):
    """
    Sub-classes gtk.Action so that we can have submenus
    """

    __gtype_name__ = "RabbitVCSAction"

    def __init__(self, name, label, tooltip, stock_id, sub_actions):
        gtk.Action.__init__(self, name, label, tooltip, stock_id)
        self.sub_actions = sub_actions

    def __repr__(self):
        return self.get_name()

    def do_create_menu_item(self):
        menu_item = gtk.ImageMenuItem()
        
        if self.sub_actions is not None:
            menu = gtk.Menu()
            menu_item.set_submenu(menu)
            
            for sub_action in self.sub_actions:
                subitem = sub_action.create_menu_item()
                menu.append(subitem)
                subitem.show()

        return menu_item

from rabbitvcs.lib.contextmenuitems import *
        
class ActionBuilder(object):

    def __init__(self, structure, conditions, callbacks):
        # The index is mostly for identifier magic 
        index = 0
        last_level = -1
        last_item = None        

        stack = [] # ([actions], last_item)

        flat_structure = rabbitvcs.lib.helper.walk_tree_depth_first(
                                structure,
                                show_levels=True,
                                preprocess=lambda x: x(conditions, callbacks),
                                filter=lambda x: x.show())
       
        
        # Here's how this works: we walk the tree, which is a series of (level,
        # MenuItem instance) tuples. We accumulate actions in the list in
        # stack[level][0], and when we go back up a level we put them in a
        # submenu (as defined by the subclasses). We need to keep track of the
        # last item on each level, in case they are separators, so that's on the
        # stack too.       
        for (level, item) in flat_structure:
            index += 1

            # Have we dropped back a level? Restore previous context
            if level < last_level:
                # We may have ended up descending several levels (it works, but
                # please no-one write triply nested menus, it's just dumb).
                for num in range(last_level - level):
                    # Remove separators at the end of menus
                    if type(last_item) == MenuSeparator:
                        stack[-1][0].remove(last_item)
                                    
                    (actions, last_item) = stack.pop()
                    
                    # Every time we back out of a level, we attach the list of
                    # actions as a submenu, however the subclass wants to do it.                    
                    action = self.make_action(last_item, index, actions)
                    if last_item.signals:
                        for signal, info in last_item.signals.items():
                            action.connect(signal, info["callback"], info["args"])
                            
                    stack[-1][0].append(action)

            # Have we gone up a level? Save the context and create a submenu
            if level > last_level:
                # Skip separators at the start of a menu
                if type(item) == MenuSeparator: continue
                
                stack.append(([], last_item))
                
                last_item = None
        
            # Skip duplicate separators
            if (type(last_item) == type(item) == MenuSeparator and
                level == last_level):
                continue

            if level == last_level:
                action = self.make_action(last_item, index, None)
                if last_item.signals:
                    for signal, info in last_item.signals.items():
                        action.connect(signal, info["callback"], info["args"])
                        
                stack[-1][0].append(action)

            last_item = item
            last_level = level



        # Hey, we're out of the loop. Go back up any remaining levels (in case
        # there were submenus at the end) and finish the job.
        for (actions, last_item2) in stack[:0:-1]:
            if type(last_item) == MenuSeparator:
                stack[-1][0].remove(last_item)
            
            action = self.make_action(last_item2, 1, actions)
            if last_item2.signals:
                for signal, info in last_item2.signals.items():
                    action.connect(signal, info["callback"], info["args"])

            stack[0][0].append(action)
            
            last_item = last_item2

        self.menu = self.top_level_menu(stack[0][0])


class ThunarxContextMenu(ActionBuilder):
    """
    Provides a standard Gtk Context Menu class used for all context menus
    in gtk dialogs/windows.
    
    """    
    def make_action(self, item, id_magic, sub_actions):
        identifier = item.make_magic_id(id_magic)
        action = RabbitVCSAction(
            identifier,
            item.label,
            item.tooltip,
            item.icon,
            sub_actions
        )

        if item.icon:
            action.set_icon_name(item.icon)

        if type(item) == MenuSeparator:
            action.set_property("sensitive", False)

        return action
    
    def top_level_menu(self, actions):
        return actions
        
    def show(self, event):        
        self.menu.show_all()
        self.menu.popup(None, None, None, event.button, event.time)

    def get_widget(self):
        return self.menu

    def get_actions(self):
        return

class ThunarxMainContextMenu(MainContextMenu):
    def get_menu(self):
        return ThunarxContextMenu(self.structure, self.conditions, self.callbacks).menu