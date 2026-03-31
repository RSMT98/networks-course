import argparse
import re
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, simpledialog, ttk
from typing import Callable, Optional

from ftp_client import FTPClient


@dataclass
class FTPListEntry:
    name: str
    is_dir: bool
    raw_line: str
    is_parent_link: bool = False


def parse_list_entry(line: str) -> FTPListEntry:
    unix_parts = line.split(maxsplit=8)
    if len(unix_parts) >= 9 and unix_parts[0]:
        first_char = unix_parts[0][0]
        if first_char in {"d", "-", "l"}:
            return FTPListEntry(
                name=unix_parts[8],
                is_dir=first_char == "d",
                raw_line=line,
            )

    windows_match = re.match(
        r"^\d{2}-\d{2}-\d{2,4}\s+\d{2}:\d{2}[AP]M\s+(<DIR>|\d+)\s+(.+)$",
        line,
    )
    if windows_match:
        return FTPListEntry(
            name=windows_match.group(2),
            is_dir=windows_match.group(1) == "<DIR>",
            raw_line=line,
        )

    return FTPListEntry(name=line.strip(), is_dir=False, raw_line=line)


class FileEditorDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        *,
        title: str,
        file_name: str,
        content: str,
        on_save: Callable[[str, str], bool],
    ) -> None:
        super().__init__(master)
        self.on_save = on_save
        self.file_name_var = tk.StringVar(value=file_name)

        self.title(title)
        self.geometry("760x520")
        self.minsize(560, 360)
        self.transient(master)
        self.grab_set()

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        top_frame = ttk.Frame(self, padding=12)
        top_frame.grid(row=0, column=0, sticky="ew")
        top_frame.columnconfigure(1, weight=1)

        ttk.Label(top_frame, text="File name:").grid(row=0, column=0, sticky="w")
        name_entry = ttk.Entry(top_frame, textvariable=self.file_name_var)
        name_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        editor_frame = ttk.Frame(self, padding=(12, 0, 12, 12))
        editor_frame.grid(row=1, column=0, sticky="nsew")
        editor_frame.columnconfigure(0, weight=1)
        editor_frame.rowconfigure(0, weight=1)

        self.text = tk.Text(editor_frame, wrap="word", undo=True)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.text.insert("1.0", content)

        text_scroll = ttk.Scrollbar(
            editor_frame, orient="vertical", command=self.text.yview
        )
        text_scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=text_scroll.set)

        buttons_frame = ttk.Frame(self, padding=(12, 0, 12, 12))
        buttons_frame.grid(row=2, column=0, sticky="e")

        ttk.Button(buttons_frame, text="Save", command=self.save).grid(
            row=0, column=0, padx=(0, 8)
        )
        ttk.Button(buttons_frame, text="Cancel", command=self.destroy).grid(
            row=0, column=1
        )

        self.bind("<Control-s>", self.save)
        self.bind("<Escape>", lambda _: self.destroy())
        name_entry.focus_set()

    def save(self, *_: object) -> None:
        file_name = self.file_name_var.get().strip()
        if not file_name:
            messagebox.showerror(
                "Validation error",
                "File name can not be empty.",
                parent=self,
            )
            return

        content = self.text.get("1.0", "end-1c")
        try:
            should_close = self.on_save(file_name, content)
        except Exception as e:
            messagebox.showerror("FTP error", str(e), parent=self)
            return

        if should_close:
            self.destroy()


class FTPClientGUI(tk.Tk):
    def __init__(
        self, host: str, port: int, user: str, password: str, force_active_ftp: bool
    ) -> None:
        super().__init__()

        self.client: Optional[FTPClient] = None
        self.entries: list[FTPListEntry] = []
        self.shown_file_name: Optional[str] = None
        self.force_active_ftp = force_active_ftp

        self.host_var = tk.StringVar(value=host)
        self.port_var = tk.StringVar(value=str(port))
        self.user_var = tk.StringVar(value=user)
        self.password_var = tk.StringVar(value=password)
        self.cur_path_var = tk.StringVar(value="/")
        self.content_title_var = tk.StringVar(value="File content")
        self.status_var = tk.StringVar(
            value="Not connected. Fill in the connection settings and press Connect."
        )

        self.title("GUI FTP client")
        self.geometry("1120x700")
        self.minsize(920, 560)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        self._build_connection_frame()
        self._build_toolbar()
        self._build_main_area()
        self._build_status_bar()
        self._update_controls()

        self.protocol("WM_DELETE_WINDOW", self.close_app)

    def _build_connection_frame(self) -> None:
        connection_frame = ttk.LabelFrame(self, text="Connection", padding=12)
        connection_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
        connection_frame.columnconfigure(1, weight=1)
        connection_frame.columnconfigure(3, weight=0)
        connection_frame.columnconfigure(5, weight=1)
        connection_frame.columnconfigure(7, weight=1)

        ttk.Label(connection_frame, text="Host:").grid(row=0, column=0, sticky="w")
        host_entry = ttk.Entry(connection_frame, textvariable=self.host_var)
        host_entry.grid(row=0, column=1, sticky="ew", padx=(6, 12))

        ttk.Label(connection_frame, text="Port:").grid(row=0, column=2, sticky="w")
        port_entry = ttk.Entry(connection_frame, width=8, textvariable=self.port_var)
        port_entry.grid(row=0, column=3, sticky="w", padx=(6, 12))

        ttk.Label(connection_frame, text="User:").grid(row=0, column=4, sticky="w")
        user_entry = ttk.Entry(connection_frame, textvariable=self.user_var)
        user_entry.grid(row=0, column=5, sticky="ew", padx=(6, 12))

        ttk.Label(connection_frame, text="Password:").grid(row=0, column=6, sticky="w")
        password_entry = ttk.Entry(
            connection_frame,
            textvariable=self.password_var,
            show="*",
        )
        password_entry.grid(row=0, column=7, sticky="ew", padx=(6, 12))

        self.connect_button = ttk.Button(
            connection_frame, text="Connect", command=self.connect_to_server
        )
        self.connect_button.grid(row=0, column=8, padx=(0, 8))

        self.disconnect_button = ttk.Button(
            connection_frame, text="Disconnect", command=self.disconnect_from_server
        )
        self.disconnect_button.grid(row=0, column=9)

        for widget in (host_entry, port_entry, user_entry, password_entry):
            widget.bind("<Return>", lambda _: self.connect_to_server())

    def _build_toolbar(self) -> None:
        toolbar = ttk.Frame(self, padding=(12, 0, 12, 8))
        toolbar.grid(row=1, column=0, sticky="ew")
        toolbar.columnconfigure(1, weight=1)

        ttk.Label(toolbar, text="Current path:").grid(row=0, column=0, sticky="w")
        ttk.Label(toolbar, textvariable=self.cur_path_var).grid(
            row=0, column=1, sticky="w", padx=(8, 16)
        )

        self.refresh_button = ttk.Button(
            toolbar, text="Refresh", command=self.refresh_entries
        )
        self.refresh_button.grid(row=0, column=2, padx=(0, 8))

        self.new_file_button = ttk.Button(
            toolbar, text="New file", command=self.open_create_dialog
        )
        self.new_file_button.grid(row=0, column=3, padx=(0, 8))

        self.new_dir_button = ttk.Button(
            toolbar, text="New folder", command=self.create_folder
        )
        self.new_dir_button.grid(row=0, column=4, padx=(0, 8))

        self.retrieve_button = ttk.Button(
            toolbar, text="Retrieve", command=self.retrieve_selected
        )
        self.retrieve_button.grid(row=0, column=5, padx=(0, 8))

        self.update_button = ttk.Button(
            toolbar, text="Update", command=self.open_update_dialog
        )
        self.update_button.grid(row=0, column=6, padx=(0, 8))

        self.delete_button = ttk.Button(
            toolbar, text="Delete", command=self.delete_selected
        )
        self.delete_button.grid(row=0, column=7)

    def _build_main_area(self) -> None:
        panes = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        panes.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 8))

        left_frame = ttk.LabelFrame(panes, text="Remote files", padding=8)
        left_frame.columnconfigure(0, weight=1)
        left_frame.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            left_frame,
            columns=("type",),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="Name")
        self.tree.heading("type", text="Type")
        self.tree.column("#0", width=360, stretch=True)
        self.tree.column("type", width=110, anchor="center", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")

        tree_scroll = ttk.Scrollbar(
            left_frame, orient="vertical", command=self.tree.yview
        )
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tree_scroll.set)

        ttk.Label(
            left_frame,
            text="Double-click a folder to open it, or double-click a file to retrieve it.",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        right_frame = ttk.LabelFrame(panes, text="Retrieve result", padding=8)
        right_frame.columnconfigure(0, weight=1)
        right_frame.rowconfigure(1, weight=1)

        ttk.Label(right_frame, textvariable=self.content_title_var).grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )

        self.content_text = tk.Text(right_frame, wrap="word")
        self.content_text.grid(row=1, column=0, sticky="nsew")

        content_scroll = ttk.Scrollbar(
            right_frame, orient="vertical", command=self.content_text.yview
        )
        content_scroll.grid(row=1, column=1, sticky="ns")
        self.content_text.configure(yscrollcommand=content_scroll.set)

        panes.add(left_frame, weight=1)
        panes.add(right_frame, weight=2)

        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_controls())
        self.tree.bind("<Double-1>", self.activate_selected)
        self.tree.bind("<Return>", self.activate_selected)

    def _build_status_bar(self) -> None:
        ttk.Label(
            self,
            textvariable=self.status_var,
            anchor="w",
            relief="sunken",
            padding=(8, 6),
        ).grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))

    def _update_controls(self) -> None:
        is_connected = self.client is not None
        selected_entry = self.get_selected_entry()
        is_file_selected = selected_entry is not None and not selected_entry.is_dir
        can_delete_selected = (
            selected_entry is not None and not selected_entry.is_parent_link
        )

        self.connect_button.configure(state="disabled" if is_connected else "normal")
        self.disconnect_button.configure(state="normal" if is_connected else "disabled")
        self.refresh_button.configure(state="normal" if is_connected else "disabled")
        self.new_file_button.configure(state="normal" if is_connected else "disabled")
        self.new_dir_button.configure(state="normal" if is_connected else "disabled")
        self.retrieve_button.configure(
            state="normal" if is_file_selected else "disabled"
        )
        self.update_button.configure(state="normal" if is_file_selected else "disabled")
        self.delete_button.configure(
            state="normal" if can_delete_selected else "disabled"
        )

    def get_selected_entry(self) -> Optional[FTPListEntry]:
        selection = self.tree.selection()
        if not selection:
            return None

        idx = int(selection[0])
        if 0 <= idx < len(self.entries):
            return self.entries[idx]
        return None

    def get_client(self) -> FTPClient:
        if self.client is None:
            raise RuntimeError("FTP client is not connected.")
        return self.client

    def clear_file_content(self) -> None:
        self.shown_file_name = None
        self.content_title_var.set("File content")
        self.content_text.delete("1.0", "end")

    def connect_to_server(self) -> None:
        if self.client is not None:
            return

        host = self.host_var.get().strip()
        user = self.user_var.get().strip()
        password = self.password_var.get()

        if not host:
            messagebox.showerror("Validation error", "Host can not be empty.")
            return

        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("Validation error", "Port must be an integer.")
            return

        client = FTPClient(force_active_ftp=self.force_active_ftp)
        try:
            client.connect(host, port)
            client.login(user, password)
            self.client = client
            if user:
                self.status_var.set(f"Connected to {host}:{port} as {user}.")
            else:
                self.status_var.set(f"Connected to {host}:{port}.")
            self.refresh_entries()
        except Exception as e:
            client.close()
            messagebox.showerror("Connection error", str(e))
            self.status_var.set(f"Connection failed: {e}")
        finally:
            self._update_controls()

    def disconnect_from_server(self) -> None:
        if self.client is None:
            return

        try:
            self.client.quit()
        except Exception:
            self.client.close()
        finally:
            self.client = None

        self.entries = []
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self.cur_path_var.set("/")
        self.clear_file_content()
        self.status_var.set("Disconnected.")
        self._update_controls()

    def show_list(self, str_list: str) -> None:
        self.entries = []
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)

        if str_list.strip():
            for line in str_list.splitlines():
                stripped_line = line.rstrip()
                if not stripped_line.strip():
                    continue

                entry = parse_list_entry(stripped_line)
                if entry.name in {".", ".."}:
                    continue

                self.entries.append(entry)

        self.entries.sort(key=lambda entry: (not entry.is_dir, entry.name.lower()))
        if self.cur_path_var.get() != "/":
            self.entries.insert(
                0,
                FTPListEntry(name="..", is_dir=True, raw_line="", is_parent_link=True),
            )

        for idx, entry in enumerate(self.entries):
            self.tree.insert(
                "",
                "end",
                iid=str(idx),
                text=entry.name,
                values=(
                    "up" if entry.is_parent_link else "dir" if entry.is_dir else "file",
                ),
            )

        self._update_controls()

    def refresh_entries(self) -> None:
        try:
            client = self.get_client()
            self.cur_path_var.set(client.pwd())
            self.show_list(client.list(""))
            if self.entries:
                self.status_var.set(
                    f"Loaded {len(self.entries)} item(s) from {self.cur_path_var.get()}."
                )
            else:
                self.status_var.set(
                    f"The directory {self.cur_path_var.get()} is empty."
                )
        except Exception as e:
            messagebox.showerror("FTP error", str(e))
            self.status_var.set(f"Refresh failed: {e}")

    def activate_selected(self, _: object = None) -> None:
        entry = self.get_selected_entry()
        if entry is None:
            return

        if entry.is_parent_link:
            try:
                self.get_client().cwd("..")
                self.refresh_entries()
            except Exception as e:
                messagebox.showerror("FTP error", str(e))
                self.status_var.set(f"Cannot change directory: {e}")
            return

        if entry.is_dir:
            try:
                self.get_client().cwd(entry.name)
                self.refresh_entries()
            except Exception as e:
                messagebox.showerror("FTP error", str(e))
                self.status_var.set(f"Cannot open directory {entry.name}: {e}")
            return

        self.retrieve_selected()

    def decode_file_content(self, data: bytes) -> str:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")

    def retrieve_selected(self) -> None:
        entry = self.get_selected_entry()
        if entry is None or entry.is_dir:
            return

        try:
            data = self.get_client().download_bytes(entry.name)
            text = self.decode_file_content(data)
            self.shown_file_name = entry.name
            self.content_title_var.set(f"File content: {entry.name}")
            self.content_text.delete("1.0", "end")
            self.content_text.insert("1.0", text)
            self.status_var.set(f"Retrieved {entry.name}.")
        except Exception as e:
            messagebox.showerror("FTP error", str(e))
            self.status_var.set(f"Retrieve failed: {e}")

    def open_create_dialog(self) -> None:
        if self.client is None:
            return

        FileEditorDialog(
            self,
            title="Create file",
            file_name="new_file.txt",
            content="",
            on_save=self.create_file,
        )

    def create_file(self, file_name: str, content: str) -> bool:
        existing_entry = next(
            (
                entry
                for entry in self.entries
                if not entry.is_dir and entry.name == file_name
            ),
            None,
        )
        if existing_entry is not None:
            should_overwrite = messagebox.askyesno(
                "Overwrite file",
                f"The file {file_name!r} already exists. Overwrite it?",
            )
            if not should_overwrite:
                return False

        self.get_client().upload_bytes(content.encode("utf-8"), file_name)
        self.refresh_entries()
        self.shown_file_name = file_name
        self.content_title_var.set(f"File content: {file_name}")
        self.content_text.delete("1.0", "end")
        self.content_text.insert("1.0", content)
        self.status_var.set(f"Saved {file_name}.")
        return True

    def open_update_dialog(self) -> None:
        entry = self.get_selected_entry()
        if entry is None or entry.is_dir:
            return

        try:
            data = self.get_client().download_bytes(entry.name)
            text = self.decode_file_content(data)
        except Exception as e:
            messagebox.showerror("FTP error", str(e))
            self.status_var.set(f"Cannot load {entry.name} for update: {e}")
            return

        FileEditorDialog(
            self,
            title=f"Update file: {entry.name}",
            file_name=entry.name,
            content=text,
            on_save=lambda file_name, content: self.update_file(
                old_file_name=entry.name,
                new_file_name=file_name,
                content=content,
            ),
        )

    def update_file(
        self, *, old_file_name: str, new_file_name: str, content: str
    ) -> bool:
        if new_file_name != old_file_name:
            should_continue = messagebox.askyesno(
                "Save as another file",
                (
                    "The file name was changed. "
                    f"The content will be saved to {new_file_name!r}, "
                    f"while {old_file_name!r} will stay unchanged. Continue?"
                ),
            )
            if not should_continue:
                return False

        self.get_client().upload_bytes(content.encode("utf-8"), new_file_name)
        self.refresh_entries()
        self.shown_file_name = new_file_name
        self.content_title_var.set(f"File content: {new_file_name}")
        self.content_text.delete("1.0", "end")
        self.content_text.insert("1.0", content)
        self.status_var.set(f"Updated {new_file_name}.")
        return True

    def create_folder(self) -> None:
        if self.client is None:
            return

        folder_name = simpledialog.askstring("New folder", "Folder name:", parent=self)
        if folder_name is None:
            return

        folder_name = folder_name.strip()
        if not folder_name:
            messagebox.showerror("Validation error", "Folder name can not be empty.")
            return

        try:
            self.get_client().mkdir(folder_name)
            self.refresh_entries()
            self.status_var.set(f"Created folder {folder_name}.")
        except Exception as e:
            messagebox.showerror("FTP error", str(e))
            self.status_var.set(f"Folder creation failed: {e}")

    def delete_selected(self) -> None:
        entry = self.get_selected_entry()
        if entry is None:
            return

        target_kind = "folder" if entry.is_dir else "file"
        should_delete = messagebox.askyesno(
            "Delete",
            f"Delete {target_kind} {entry.name!r}?",
            parent=self,
        )
        if not should_delete:
            return

        try:
            client = self.get_client()
            if entry.is_dir:
                client.remove_dir(entry.name)
            else:
                client.remove_file(entry.name)
                if self.shown_file_name == entry.name:
                    self.clear_file_content()

            self.refresh_entries()
            self.status_var.set(f"Deleted {target_kind} {entry.name}.")
        except Exception as e:
            messagebox.showerror("FTP error", str(e))
            self.status_var.set(f"Delete failed: {e}")

    def close_app(self) -> None:
        self.disconnect_from_server()
        self.destroy()


parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=21)
parser.add_argument("--user", default="TestUser")
parser.add_argument("--password", default="")
parser.add_argument("--force-active-ftp", action="store_true")
args = parser.parse_args()

FTPClientGUI(
    args.host, args.port, args.user, args.password, args.force_active_ftp
).mainloop()
