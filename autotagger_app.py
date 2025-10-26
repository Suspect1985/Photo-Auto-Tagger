"""
Auto-Tagger - Automatic Photo Tagging Application
Automatically assigns date and location tags to images based on EXIF metadata.
"""

import sys
import os
import sqlite3
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Tuple, List

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QProgressBar, QTextEdit,
    QFileDialog, QMessageBox
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
import piexif


# ────────────────────────────────────────────────────────────────
# SUPPORTED IMAGE FORMATS
# ────────────────────────────────────────────────────────────────
SUPPORTED_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.webp', '.heic', '.tiff', '.tif', '.bmp', '.gif'
}


# ────────────────────────────────────────────────────────────────
# DATABASE MANAGER
# ────────────────────────────────────────────────────────────────
class DatabaseManager:
    """Manages SQLite database operations with performance optimizations."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = None

    def connect(self):
        """Connect to database and apply performance pragmas."""
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()

    def _create_schema(self):
        """Create database schema if it doesn't exist."""
        cursor = self.conn.cursor()

        # PhotoMetadata table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS PhotoMetadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_path TEXT UNIQUE NOT NULL,
                file_name TEXT,
                created_at TEXT,
                location TEXT,
                rotation INTEGER DEFAULT 0
            )
        """)

        # Tags table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
        """)

        # Photo_Tags_Link junction table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Photo_Tags_Link (
                photo_id INTEGER,
                tag_id INTEGER,
                FOREIGN KEY (photo_id) REFERENCES PhotoMetadata (id),
                FOREIGN KEY (tag_id) REFERENCES Tags (id),
                UNIQUE (photo_id, tag_id)
            )
        """)

        # Create indexes
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_photo_id
            ON Photo_Tags_Link(photo_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tag_id
            ON Photo_Tags_Link(tag_id)
        """)

        self.conn.commit()

    def get_or_create_tag(self, tag_name: str) -> int:
        """Get tag ID or create if doesn't exist. Returns tag ID."""
        cursor = self.conn.cursor()

        # Try to get existing tag
        cursor.execute("SELECT id FROM Tags WHERE name = ?", (tag_name,))
        result = cursor.fetchone()

        if result:
            return result[0]

        # Create new tag
        cursor.execute("INSERT INTO Tags (name) VALUES (?)", (tag_name,))
        return cursor.lastrowid

    def upsert_photo(self, image_path: str, file_name: str,
                     created_at: str, location: str) -> int:
        """Insert or update photo metadata. Returns photo ID."""
        cursor = self.conn.cursor()

        cursor.execute("""
            INSERT INTO PhotoMetadata (image_path, file_name, created_at, location)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(image_path) DO UPDATE SET
                file_name = excluded.file_name,
                created_at = excluded.created_at,
                location = excluded.location
        """, (image_path, file_name, created_at, location))

        # Get the photo ID
        cursor.execute("SELECT id FROM PhotoMetadata WHERE image_path = ?", (image_path,))
        return cursor.fetchone()[0]

    def link_photo_tag(self, photo_id: int, tag_id: int):
        """Create link between photo and tag (if not exists)."""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO Photo_Tags_Link (photo_id, tag_id)
            VALUES (?, ?)
        """, (photo_id, tag_id))

    def begin_transaction(self):
        """Start a database transaction for batch operations."""
        self.conn.execute("BEGIN TRANSACTION")

    def commit(self):
        """Commit the current transaction."""
        self.conn.commit()

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()


# ────────────────────────────────────────────────────────────────
# EXIF METADATA EXTRACTOR
# ────────────────────────────────────────────────────────────────
class ExifExtractor:
    """Extract date and GPS metadata from image files."""

    @staticmethod
    def extract_metadata(image_path: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract creation date and location from image.
        Returns: (creation_date_str, location_str)
        """
        try:
            # Try to get EXIF data
            with Image.open(image_path) as img:
                exif_data = img._getexif()

            creation_date = None
            location = None

            # Extract creation date
            if exif_data:
                creation_date = ExifExtractor._extract_date(exif_data, image_path)
                location = ExifExtractor._extract_location(exif_data)

            # Fallback to file modification time if no EXIF date
            if not creation_date:
                creation_date = ExifExtractor._get_file_creation_date(image_path)

            return creation_date, location

        except Exception as e:
            # Fallback for files without EXIF or errors
            creation_date = ExifExtractor._get_file_creation_date(image_path)
            return creation_date, None

    @staticmethod
    def _extract_date(exif_data: dict, image_path: str) -> Optional[str]:
        """Extract creation date from EXIF data."""
        # Try different date tags
        date_tags = [
            36867,  # DateTimeOriginal
            36868,  # DateTimeDigitized
            306,    # DateTime
        ]

        for tag in date_tags:
            if tag in exif_data:
                date_str = exif_data[tag]
                try:
                    # EXIF date format: "YYYY:MM:DD HH:MM:SS"
                    dt = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
                    return dt.isoformat()
                except:
                    continue

        return None

    @staticmethod
    def _extract_location(exif_data: dict) -> Optional[str]:
        """Extract GPS location from EXIF data and format as 'Lat, Long'."""
        try:
            # GPS info is in tag 34853
            if 34853 not in exif_data:
                return None

            gps_info = exif_data[34853]

            # Extract latitude
            if 2 not in gps_info or 1 not in gps_info:
                return None
            lat = ExifExtractor._convert_to_degrees(gps_info[2])
            if gps_info[1] == 'S':
                lat = -lat

            # Extract longitude
            if 4 not in gps_info or 3 not in gps_info:
                return None
            lon = ExifExtractor._convert_to_degrees(gps_info[4])
            if gps_info[3] == 'W':
                lon = -lon

            return f"{lat:.6f}, {lon:.6f}"

        except Exception:
            return None

    @staticmethod
    def _convert_to_degrees(value) -> float:
        """Convert GPS coordinates to degrees."""
        d, m, s = value
        return float(d) + float(m) / 60.0 + float(s) / 3600.0

    @staticmethod
    def _get_file_creation_date(image_path: str) -> str:
        """Get file creation/modification time as fallback."""
        try:
            # Use modification time
            mtime = os.path.getmtime(image_path)
            dt = datetime.fromtimestamp(mtime)
            return dt.isoformat()
        except:
            return datetime.now().isoformat()


# ────────────────────────────────────────────────────────────────
# WORKER THREAD
# ────────────────────────────────────────────────────────────────
class AutoTaggerWorker(QThread):
    """Worker thread for scanning and tagging images."""

    # Signals for UI updates
    progress_update = pyqtSignal(int, int)  # (current, total)
    phase_update = pyqtSignal(str)          # phase description
    log_message = pyqtSignal(str)           # log entry
    finished = pyqtSignal(int, int, int)    # (total_photos, total_tags, errors)

    def __init__(self, folder_path: str, db_path: str):
        super().__init__()
        self.folder_path = folder_path
        self.db_path = db_path
        self.is_cancelled = False

    def cancel(self):
        """Cancel the worker thread."""
        self.is_cancelled = True

    def run(self):
        """Main worker thread execution."""
        total_photos = 0
        total_tags = 0
        errors = 0

        try:
            # Initialize database
            self.log_message.emit(f"Connecting to database: {self.db_path}")
            db = DatabaseManager(self.db_path)
            db.connect()

            # Scan for image files
            self.phase_update.emit("Scanning for images...")
            self.log_message.emit(f"Scanning folder: {self.folder_path}")

            image_files = self._scan_images(self.folder_path)
            total_files = len(image_files)

            if total_files == 0:
                self.log_message.emit("No image files found.")
                self.finished.emit(0, 0, 0)
                db.close()
                return

            self.log_message.emit(f"Found {total_files} image files.")

            # Phase 1: Extract metadata and insert photos
            self.phase_update.emit("Extracting metadata...")
            metadata_list = []

            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {
                    executor.submit(ExifExtractor.extract_metadata, img): img
                    for img in image_files
                }

                for i, future in enumerate(as_completed(futures)):
                    if self.is_cancelled:
                        self.log_message.emit("Operation cancelled.")
                        break

                    img_path = futures[future]
                    try:
                        created_at, location = future.result()
                        metadata_list.append((img_path, created_at, location))
                        self.progress_update.emit(i + 1, total_files)
                    except Exception as e:
                        self.log_message.emit(f"Error reading {os.path.basename(img_path)}: {str(e)}")
                        errors += 1

            if self.is_cancelled:
                db.close()
                return

            # Phase 2: Insert photos and create tags (batched)
            self.phase_update.emit("Creating database entries...")
            self.log_message.emit("Inserting photos into database...")

            db.begin_transaction()

            photo_tags = []  # List of (photo_id, year_tag, location_tag)

            for i, (img_path, created_at, location) in enumerate(metadata_list):
                try:
                    file_name = os.path.basename(img_path)
                    location_str = location if location else "Unknown Location"

                    # Insert/update photo
                    photo_id = db.upsert_photo(
                        image_path=img_path,
                        file_name=file_name,
                        created_at=created_at,
                        location=location_str
                    )

                    # Extract year for date tag
                    if created_at:
                        year = created_at[:4]
                    else:
                        year = str(datetime.now().year)

                    photo_tags.append((photo_id, year, location_str))
                    total_photos += 1

                    self.progress_update.emit(i + 1, len(metadata_list))

                except Exception as e:
                    self.log_message.emit(f"Error inserting {os.path.basename(img_path)}: {str(e)}")
                    errors += 1

            db.commit()

            if self.is_cancelled:
                db.close()
                return

            # Phase 3: Create tags and link to photos
            self.phase_update.emit("Creating tags...")
            self.log_message.emit("Creating and linking tags...")

            db.begin_transaction()

            tag_cache = {}  # Cache tag IDs to avoid repeated lookups

            for i, (photo_id, year, location) in enumerate(photo_tags):
                try:
                    # Get or create year tag
                    if year not in tag_cache:
                        tag_cache[year] = db.get_or_create_tag(year)
                    year_tag_id = tag_cache[year]

                    # Get or create location tag
                    if location not in tag_cache:
                        tag_cache[location] = db.get_or_create_tag(location)
                    location_tag_id = tag_cache[location]

                    # Link photo to tags
                    db.link_photo_tag(photo_id, year_tag_id)
                    db.link_photo_tag(photo_id, location_tag_id)

                    self.progress_update.emit(i + 1, len(photo_tags))

                except Exception as e:
                    self.log_message.emit(f"Error linking tags: {str(e)}")
                    errors += 1

            db.commit()

            total_tags = len(tag_cache)
            self.log_message.emit(f"Tagging complete! {total_photos} photos, {total_tags} unique tags.")

            db.close()

        except Exception as e:
            self.log_message.emit(f"Fatal error: {str(e)}")
            errors += 1

        finally:
            self.finished.emit(total_photos, total_tags, errors)

    def _scan_images(self, folder_path: str) -> List[str]:
        """Recursively scan folder for supported image files."""
        image_files = []

        for root, dirs, files in os.walk(folder_path):
            for file in files:
                ext = Path(file).suffix.lower()
                if ext in SUPPORTED_EXTENSIONS:
                    full_path = os.path.join(root, file)
                    image_files.append(full_path)

        return image_files


# ────────────────────────────────────────────────────────────────
# MAIN WINDOW
# ────────────────────────────────────────────────────────────────
class AutoTaggerWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.worker = None
        self.init_ui()

    def init_ui(self):
        """Initialize the user interface."""
        self.setWindowTitle("Auto-Tagger")
        self.setGeometry(100, 100, 800, 600)

        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Main layout
        layout = QVBoxLayout()
        layout.setSpacing(10)

        # Title
        title_label = QLabel("Auto-Tagger - Automatic Photo Tagging")
        title_label.setStyleSheet("font-size: 18px; font-weight: bold;")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)

        # Folder selection
        folder_layout = QHBoxLayout()
        folder_label = QLabel("Folder:")
        self.folder_input = QLineEdit()
        self.folder_input.setPlaceholderText("Select a folder containing images...")
        self.browse_button = QPushButton("Browse")
        self.browse_button.clicked.connect(self.browse_folder)

        folder_layout.addWidget(folder_label)
        folder_layout.addWidget(self.folder_input, stretch=1)
        folder_layout.addWidget(self.browse_button)
        layout.addLayout(folder_layout)

        # Buttons
        button_layout = QHBoxLayout()
        self.start_button = QPushButton("Start Auto-Tag")
        self.start_button.clicked.connect(self.start_tagging)
        self.start_button.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold; padding: 10px;")

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_tagging)
        self.cancel_button.setEnabled(False)
        self.cancel_button.setStyleSheet("background-color: #f44336; color: white; font-weight: bold; padding: 10px;")

        button_layout.addWidget(self.start_button)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Phase label
        self.phase_label = QLabel("Ready to start...")
        self.phase_label.setStyleSheet("font-style: italic; color: #555;")
        layout.addWidget(self.phase_label)

        # Log output
        log_label = QLabel("Log:")
        layout.addWidget(log_label)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("background-color: #f5f5f5; font-family: monospace;")
        layout.addWidget(self.log_text)

        central_widget.setLayout(layout)

    def browse_folder(self):
        """Open folder selection dialog."""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Photo Folder",
            "",
            QFileDialog.Option.ShowDirsOnly
        )

        if folder:
            self.folder_input.setText(folder)

    def start_tagging(self):
        """Start the auto-tagging process."""
        folder_path = self.folder_input.text().strip()

        if not folder_path:
            QMessageBox.warning(self, "No Folder", "Please select a folder first.")
            return

        if not os.path.isdir(folder_path):
            QMessageBox.warning(self, "Invalid Folder", "The selected folder does not exist.")
            return

        # Clear log
        self.log_text.clear()
        self.progress_bar.setValue(0)

        # Database path
        db_path = os.path.join(folder_path, "photo_library.db")

        # Disable start button, enable cancel
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.browse_button.setEnabled(False)

        # Create and start worker
        self.worker = AutoTaggerWorker(folder_path, db_path)
        self.worker.progress_update.connect(self.update_progress)
        self.worker.phase_update.connect(self.update_phase)
        self.worker.log_message.connect(self.add_log)
        self.worker.finished.connect(self.tagging_finished)
        self.worker.start()

    def cancel_tagging(self):
        """Cancel the current tagging operation."""
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.add_log("Cancelling operation...")
            self.cancel_button.setEnabled(False)

    def update_progress(self, current: int, total: int):
        """Update progress bar."""
        if total > 0:
            percentage = int((current / total) * 100)
            self.progress_bar.setValue(percentage)

    def update_phase(self, phase: str):
        """Update phase label."""
        self.phase_label.setText(phase)

    def add_log(self, message: str):
        """Add message to log."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{timestamp}] {message}")

    def tagging_finished(self, total_photos: int, total_tags: int, errors: int):
        """Handle completion of tagging."""
        # Re-enable buttons
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.browse_button.setEnabled(True)

        # Update UI
        self.progress_bar.setValue(100)
        self.phase_label.setText("Complete!")

        # Show completion message
        if errors > 0:
            message = f"Tagging complete with {errors} errors.\n\n"
        else:
            message = "Tagging complete!\n\n"

        message += f"Photos processed: {total_photos}\n"
        message += f"Unique tags created: {total_tags}"

        QMessageBox.information(self, "Auto-Tagger Complete", message)


# ────────────────────────────────────────────────────────────────
# APPLICATION ENTRY POINT
# ────────────────────────────────────────────────────────────────
def main():
    """Application entry point."""
    app = QApplication(sys.argv)
    app.setStyle('Fusion')  # Modern cross-platform style

    window = AutoTaggerWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
