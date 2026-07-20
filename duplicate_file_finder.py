"""
============================================================================
 Duplicate File Finder
 ----------------------------------------------------------------------------
 Concepts : File Handling + Regular Expressions      <-- do NOT change
 GUI      : PyQt5
 Level    : Advanced
============================================================================

 REAL-WORLD PROBLEM
 ------------------
 Over time a computer collects the same file many times over - photos copied
 from a phone twice, "report_final.pdf" saved again as "report_final(1).pdf",
 downloads repeated, backup folders duplicated. These copies waste disk space
 and make folders hard to search.

 This program scans a folder (and its sub-folders), finds files whose CONTENTS
 are byte-for-byte identical, groups them together, and reports exactly how
 much space is being wasted. The user can then delete the extra copies while
 always keeping one original.

 HOW DUPLICATES ARE DETECTED
 ---------------------------
 Comparing every file with every other file would be extremely slow, so the
 scan runs in two passes:

   PASS 1 - group by SIZE.  Two files with different sizes can never be
            identical, so any file with a unique size is discarded straight
            away. This is very fast because a size is read from the file
            system without opening the file.

   PASS 2 - group by HASH.  Only files that share a size are actually read.
            Each one is read in small CHUNKS (never all at once, so a huge
            file cannot exhaust memory) and fed into a SHA-256 hash. Files
            producing the same hash have identical contents.

 REGULAR EXPRESSIONS
 -------------------
 Two optional regex filters control which files are scanned:
   * Include pattern - only file names matching this regex are considered.
   * Exclude pattern - file names matching this regex are skipped.
 Example: include  \\.(jpg|png)$   to scan only images,
          exclude  ^~\\$          to skip temporary Office files.
============================================================================
"""

import hashlib
import os
import re
import sys

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)


# ===========================================================================
#  PART 1: CORE LOGIC  (no GUI code here, so it is easy to read and test)
# ===========================================================================
CHUNK_SIZE = 65536          # read files 64 KB at a time


def human_size(num_bytes):
    """Turn a byte count into a readable string such as '1.4 MB'."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def hash_file(path, chunk_size=CHUNK_SIZE):
    """Return the SHA-256 hash of a file's contents.

    The file is read in small chunks in binary mode ('rb') so that even a
    very large file never has to fit into memory all at once.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:               # b"" means end of file
                break
            digest.update(chunk)
    return digest.hexdigest()


def compile_pattern(pattern):
    """Compile a regex, or return None if the box was left empty.

    Raises re.error if the pattern is invalid, so the GUI can warn the user.
    """
    pattern = (pattern or "").strip()
    if not pattern:
        return None
    return re.compile(pattern, re.IGNORECASE)


def name_allowed(filename, include_re, exclude_re):
    """Decide whether a file name passes the include / exclude regex filters."""
    if include_re is not None and not include_re.search(filename):
        return False
    if exclude_re is not None and exclude_re.search(filename):
        return False
    return True


def collect_files(folder, include_re=None, exclude_re=None, recursive=True):
    """Walk the folder and return a list of file paths passing the filters."""
    collected = []
    for root, dirs, files in os.walk(folder):
        if not recursive:
            dirs.clear()                # stop os.walk descending any further
        for filename in files:
            if not name_allowed(filename, include_re, exclude_re):
                continue
            full_path = os.path.join(root, filename)
            if os.path.isfile(full_path) and not os.path.islink(full_path):
                collected.append(full_path)
    return collected


def group_by_size(paths):
    """PASS 1 - bucket paths by file size, keeping only sizes seen 2+ times."""
    sizes = {}
    for path in paths:
        try:
            size = os.path.getsize(path)
        except OSError:
            continue                    # unreadable / vanished file - skip it
        sizes.setdefault(size, []).append(path)
    return {size: group for size, group in sizes.items() if len(group) > 1}


def find_duplicates(folder, include_pattern="", exclude_pattern="",
                    recursive=True, progress=None, should_stop=None):
    """Find groups of files with identical contents.

    progress    - optional callback(done, total, message) for the GUI
    should_stop - optional callable returning True to cancel early

    Returns a list of groups; each group is a list of paths that are
    byte-for-byte identical, and every group has at least 2 files.
    """
    include_re = compile_pattern(include_pattern)
    exclude_re = compile_pattern(exclude_pattern)

    if progress:
        progress(0, 0, "Collecting files...")
    all_files = collect_files(folder, include_re, exclude_re, recursive)

    size_groups = group_by_size(all_files)
    candidates = [p for group in size_groups.values() for p in group]
    total = len(candidates)

    # PASS 2 - hash only the files that share a size with another file.
    hashes = {}
    for index, path in enumerate(candidates, start=1):
        if should_stop and should_stop():
            break
        try:
            file_hash = hash_file(path)
        except OSError:
            continue                    # permission denied etc. - skip it
        hashes.setdefault(file_hash, []).append(path)
        if progress:
            progress(index, total, f"Hashing {os.path.basename(path)}")

    duplicates = [group for group in hashes.values() if len(group) > 1]
    duplicates.sort(key=lambda g: os.path.getsize(g[0]), reverse=True)
    return duplicates


def wasted_space(groups):
    """Bytes that could be freed by keeping only one file from each group."""
    total = 0
    for group in groups:
        try:
            total += os.path.getsize(group[0]) * (len(group) - 1)
        except OSError:
            pass
    return total


# ===========================================================================
#  PART 2: BACKGROUND WORKER
#  Scanning can take a while. Doing it on a separate thread keeps the window
#  responsive instead of freezing until the scan finishes.
# ===========================================================================
class ScanWorker(QThread):
    progressed = pyqtSignal(int, int, str)   # done, total, message
    finished_ok = pyqtSignal(list)           # list of duplicate groups
    failed = pyqtSignal(str)                 # error message

    def __init__(self, folder, include_pattern, exclude_pattern, recursive):
        super().__init__()
        self.folder = folder
        self.include_pattern = include_pattern
        self.exclude_pattern = exclude_pattern
        self.recursive = recursive
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            groups = find_duplicates(
                self.folder,
                self.include_pattern,
                self.exclude_pattern,
                self.recursive,
                progress=lambda d, t, m: self.progressed.emit(d, t, m),
                should_stop=lambda: self._stop,
            )
            self.finished_ok.emit(groups)
        except re.error as e:
            self.failed.emit(f"Invalid regular expression: {e}")
        except Exception as e:                     # keep the GUI alive
            self.failed.emit(str(e))


# ===========================================================================
#  PART 3: GRAPHICAL USER INTERFACE  (PyQt5)
# ===========================================================================
class DuplicateFinderWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle(
            "Duplicate File Finder  -  File Handling + Regex  (Waleed Ahmad Khan)")
        self.resize(950, 640)

        self.groups = []          # the duplicate groups from the last scan
        self.worker = None

        self._build_ui()

    # ---------------------------------------------------------------- layout
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # ---- folder chooser ------------------------------------------------
        folder_box = QGroupBox("1. Choose a folder to scan")
        folder_row = QHBoxLayout(folder_box)
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("No folder selected...")
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self.choose_folder)
        folder_row.addWidget(self.folder_edit)
        folder_row.addWidget(browse_btn)
        layout.addWidget(folder_box)

        # ---- regex filters -------------------------------------------------
        filter_box = QGroupBox("2. Optional regex filters")
        filter_layout = QVBoxLayout(filter_box)

        include_row = QHBoxLayout()
        include_row.addWidget(QLabel("Include (regex):"))
        self.include_edit = QLineEdit()
        self.include_edit.setPlaceholderText(r"e.g.  \.(jpg|png|pdf)$   - leave empty for all files")
        include_row.addWidget(self.include_edit)
        filter_layout.addLayout(include_row)

        exclude_row = QHBoxLayout()
        exclude_row.addWidget(QLabel("Exclude (regex):"))
        self.exclude_edit = QLineEdit()
        self.exclude_edit.setPlaceholderText(r"e.g.  ^~\$|\.tmp$   - leave empty to exclude nothing")
        exclude_row.addWidget(self.exclude_edit)
        filter_layout.addLayout(exclude_row)

        self.recursive_check = QCheckBox("Include sub-folders")
        self.recursive_check.setChecked(True)
        filter_layout.addWidget(self.recursive_check)
        layout.addWidget(filter_box)

        # ---- action buttons ------------------------------------------------
        button_row = QHBoxLayout()
        self.scan_btn = QPushButton("Scan for Duplicates")
        self.scan_btn.clicked.connect(self.start_scan)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_scan)
        self.stop_btn.setEnabled(False)
        self.delete_btn = QPushButton("Delete Ticked Files")
        self.delete_btn.clicked.connect(self.delete_ticked)
        self.delete_btn.setEnabled(False)
        button_row.addWidget(self.scan_btn)
        button_row.addWidget(self.stop_btn)
        button_row.addStretch()
        button_row.addWidget(self.delete_btn)
        layout.addLayout(button_row)

        # ---- progress ------------------------------------------------------
        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        # ---- results tree --------------------------------------------------
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Size", "Location"])
        self.tree.setColumnWidth(0, 330)
        self.tree.setColumnWidth(1, 90)
        layout.addWidget(self.tree)

        # ---- status line ---------------------------------------------------
        self.status = QLabel("Ready. Choose a folder and press \"Scan for Duplicates\".")
        layout.addWidget(self.status)

    # ------------------------------------------------------------- actions
    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select a folder to scan")
        if folder:
            self.folder_edit.setText(folder)

    def start_scan(self):
        folder = self.folder_edit.text().strip()

        # --- edge case: no folder chosen, or it does not exist ---
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, "No folder",
                                "Please choose a valid folder to scan.")
            return

        # --- edge case: check the regexes BEFORE starting the scan ---
        for label, pattern in (("Include", self.include_edit.text()),
                               ("Exclude", self.exclude_edit.text())):
            try:
                compile_pattern(pattern)
            except re.error as e:
                QMessageBox.warning(self, "Invalid regex",
                                    f"The {label} pattern is not valid:\n{e}")
                return

        self.tree.clear()
        self.groups = []
        self.delete_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress.setValue(0)
        self.status.setText("Scanning...")

        self.worker = ScanWorker(folder, self.include_edit.text(),
                                 self.exclude_edit.text(),
                                 self.recursive_check.isChecked())
        self.worker.progressed.connect(self.on_progress)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def stop_scan(self):
        if self.worker:
            self.worker.stop()
            self.status.setText("Stopping...")

    # ------------------------------------------------------- worker signals
    def on_progress(self, done, total, message):
        if total:
            self.progress.setMaximum(total)
            self.progress.setValue(done)
        self.status.setText(message)

    def on_finished(self, groups):
        self.groups = groups
        self.scan_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.show_results(groups)

    def on_failed(self, message):
        self.scan_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText("Scan failed.")
        QMessageBox.critical(self, "Scan failed", message)

    # -------------------------------------------------------- results view
    def show_results(self, groups):
        """Fill the tree: one top-level row per duplicate group."""
        self.tree.clear()

        if not groups:
            self.status.setText("No duplicate files were found.")
            self.progress.setValue(self.progress.maximum())
            return

        for number, group in enumerate(groups, start=1):
            size = os.path.getsize(group[0])
            parent = QTreeWidgetItem([
                f"Group {number}  -  {len(group)} identical files",
                human_size(size),
                f"{human_size(size * (len(group) - 1))} can be freed",
            ])
            self.tree.addTopLevelItem(parent)

            for position, path in enumerate(group):
                child = QTreeWidgetItem([
                    os.path.basename(path), human_size(size), os.path.dirname(path)
                ])
                child.setData(0, Qt.UserRole, path)
                # The first file in each group is treated as the original and
                # is left unticked, so the user never deletes every copy.
                child.setCheckState(0, Qt.Unchecked if position == 0 else Qt.Checked)
                parent.addChild(child)

            parent.setExpanded(True)

        total_files = sum(len(g) for g in groups)
        self.delete_btn.setEnabled(True)
        self.status.setText(
            f"Found {len(groups)} duplicate group(s) covering {total_files} files. "
            f"Wasted space: {human_size(wasted_space(groups))}."
        )

    def ticked_paths(self):
        """Return every file path whose checkbox is ticked."""
        paths = []
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            for j in range(parent.childCount()):
                child = parent.child(j)
                if child.checkState(0) == Qt.Checked:
                    paths.append(child.data(0, Qt.UserRole))
        return paths

    def delete_ticked(self):
        paths = self.ticked_paths()

        # --- edge case: nothing ticked ---
        if not paths:
            QMessageBox.information(self, "Nothing selected",
                                    "Tick the copies you want to delete first.")
            return

        # --- safety: confirm before deleting anything ---
        answer = QMessageBox.question(
            self, "Confirm delete",
            f"Permanently delete {len(paths)} file(s)?\n"
            "One copy in each group is kept.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return

        deleted, failed = 0, []
        for path in paths:
            try:
                os.remove(path)
                deleted += 1
            except OSError as e:
                failed.append(f"{os.path.basename(path)}: {e}")

        self.status.setText(f"Deleted {deleted} file(s).")
        if failed:
            QMessageBox.warning(self, "Some files could not be deleted",
                                "\n".join(failed[:10]))

        # Re-scan so the list always reflects what is really on disk.
        self.start_scan()


# ===========================================================================
#  PROGRAM ENTRY POINT
# ===========================================================================
def main():
    app = QApplication(sys.argv)
    window = DuplicateFinderWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
