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
    def extract_metadata(image_path: str) -> Tuple[Optional[str], Optional[str], str]:
        """
        Extract creation date and location from image.
        Returns: (creation_date_str, location_str, debug_message)
        """
        creation_date = None
        location = None
        debug_parts = []

        # Try Method 1: Modern Pillow getexif() (no underscore)
        try:
            with Image.open(image_path) as img:
                exif_data = img.getexif()

                if exif_data and len(exif_data) > 0:
                    debug_parts.append(f"Pillow found {len(exif_data)} EXIF tags")

                    creation_date = ExifExtractor._extract_date_from_pillow(exif_data)
                    if creation_date:
                        debug_parts.append("Date extracted via Pillow")

                    location = ExifExtractor._extract_location_from_pillow(exif_data)
                    if location:
                        debug_parts.append("GPS extracted via Pillow")
                    elif 34853 in exif_data:
                        # GPS IFD exists but might not have coordinates
                        try:
                            gps_ifd = exif_data.get_ifd(34853)
                            gps_tags = list(gps_ifd.keys()) if gps_ifd else []
                            has_coords = all(tag in gps_tags for tag in [1, 2, 3, 4])
                            if has_coords:
                                debug_parts.append("GPS coords exist but failed to convert")
                            else:
                                debug_parts.append(f"GPS IFD present but no coordinates (tags: {gps_tags})")
                        except:
                            debug_parts.append("GPS tag exists but inaccessible")
                    else:
                        debug_parts.append("No GPS tag (34853)")
                else:
                    debug_parts.append("Pillow: No EXIF data")
        except Exception as e:
            debug_parts.append(f"Pillow error: {type(e).__name__}: {str(e)}")

        # Try Method 2: piexif library (better for some formats)
        if not creation_date or not location:
            try:
                exif_dict = piexif.load(image_path)

                # Check if any EXIF data exists
                has_data = any(exif_dict.get(ifd) for ifd in ["0th", "Exif", "GPS", "1st"])

                if has_data:
                    debug_parts.append("piexif found EXIF data")

                    if not creation_date:
                        creation_date = ExifExtractor._extract_date_from_piexif(exif_dict)
                        if creation_date:
                            debug_parts.append("Date extracted via piexif")

                    if not location and "GPS" in exif_dict and exif_dict["GPS"]:
                        gps_ifd = exif_dict["GPS"]
                        gps_tag_ids = list(gps_ifd.keys())
                        has_coords = all(tag in gps_tag_ids for tag in [piexif.GPSIFD.GPSLatitude,
                                                                          piexif.GPSIFD.GPSLatitudeRef,
                                                                          piexif.GPSIFD.GPSLongitude,
                                                                          piexif.GPSIFD.GPSLongitudeRef])

                        if has_coords:
                            location = ExifExtractor._extract_location_from_piexif(exif_dict)
                            if location:
                                debug_parts.append("GPS extracted via piexif")
                            else:
                                debug_parts.append("GPS coords exist but piexif conversion failed")
                        else:
                            debug_parts.append(f"GPS IFD missing coordinates (has tags: {gps_tag_ids})")
                    elif not location:
                        debug_parts.append("No GPS IFD in piexif")
                else:
                    debug_parts.append("piexif: No EXIF data")
            except Exception as e:
                debug_parts.append(f"piexif error: {type(e).__name__}: {str(e)}")

        # Fallback to file modification time if no EXIF date
        if not creation_date:
            creation_date = ExifExtractor._get_file_creation_date(image_path)
            debug_parts.append("Using file mtime")

        debug_message = " | ".join(debug_parts)
        return creation_date, location, debug_message

    @staticmethod
    def _extract_date_from_pillow(exif_data) -> Optional[str]:
        """Extract creation date from Pillow's getexif() data."""
        # Try different date tags in order of preference
        date_tags = [
            36867,  # DateTimeOriginal
            36868,  # DateTimeDigitized
            306,    # DateTime
        ]

        for tag in date_tags:
            if tag in exif_data:
                date_str = exif_data[tag]
                if date_str:
                    try:
                        # EXIF date format: "YYYY:MM:DD HH:MM:SS"
                        dt = datetime.strptime(str(date_str), "%Y:%m:%d %H:%M:%S")
                        return dt.isoformat()
                    except:
                        continue

        return None

    @staticmethod
    def _extract_date_from_piexif(exif_dict: dict) -> Optional[str]:
        """Extract creation date from piexif data."""
        try:
            # Check Exif IFD for date tags
            if "Exif" in exif_dict:
                exif_ifd = exif_dict["Exif"]

                # Try DateTimeOriginal first (most reliable)
                if piexif.ExifIFD.DateTimeOriginal in exif_ifd:
                    date_bytes = exif_ifd[piexif.ExifIFD.DateTimeOriginal]
                    date_str = date_bytes.decode('utf-8') if isinstance(date_bytes, bytes) else date_bytes
                    dt = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
                    return dt.isoformat()

                # Try DateTimeDigitized
                if piexif.ExifIFD.DateTimeDigitized in exif_ifd:
                    date_bytes = exif_ifd[piexif.ExifIFD.DateTimeDigitized]
                    date_str = date_bytes.decode('utf-8') if isinstance(date_bytes, bytes) else date_bytes
                    dt = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
                    return dt.isoformat()

            # Check main IFD for DateTime
            if "0th" in exif_dict:
                zeroth_ifd = exif_dict["0th"]
                if piexif.ImageIFD.DateTime in zeroth_ifd:
                    date_bytes = zeroth_ifd[piexif.ImageIFD.DateTime]
                    date_str = date_bytes.decode('utf-8') if isinstance(date_bytes, bytes) else date_bytes
                    dt = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
                    return dt.isoformat()

        except Exception:
            pass

        return None

    @staticmethod
    def _extract_location_from_pillow(exif_data) -> Optional[str]:
        """Extract GPS location from Pillow's getexif() data and format as 'Lat, Long'."""
        try:
            # GPS info is in tag 34853
            if 34853 not in exif_data:
                return None

            # Try to get GPS IFD - this can fail even if tag exists
            try:
                gps_info = exif_data.get_ifd(34853)
            except (KeyError, AttributeError, TypeError) as e:
                # get_ifd might not be available or GPS IFD might be malformed
                return None

            if not gps_info:
                return None

            # DEBUG: Check which GPS tags are present
            # Common GPS tags: 1=LatRef, 2=Lat, 3=LonRef, 4=Lon
            available_tags = list(gps_info.keys())

            # Extract latitude (tag 2 = GPSLatitude, tag 1 = GPSLatitudeRef)
            if 2 not in gps_info:
                return None  # Missing latitude data
            if 1 not in gps_info:
                return None  # Missing latitude reference

            lat_data = gps_info[2]
            lat = ExifExtractor._convert_to_degrees(lat_data)
            if lat is None:
                return None

            lat_ref = gps_info[1]
            # Handle both string and bytes
            if isinstance(lat_ref, bytes):
                lat_ref = lat_ref.decode('utf-8', errors='ignore')
            if lat_ref == 'S':
                lat = -lat

            # Extract longitude (tag 4 = GPSLongitude, tag 3 = GPSLongitudeRef)
            if 4 not in gps_info:
                return None  # Missing longitude data
            if 3 not in gps_info:
                return None  # Missing longitude reference

            lon_data = gps_info[4]
            lon = ExifExtractor._convert_to_degrees(lon_data)
            if lon is None:
                return None

            lon_ref = gps_info[3]
            # Handle both string and bytes
            if isinstance(lon_ref, bytes):
                lon_ref = lon_ref.decode('utf-8', errors='ignore')
            if lon_ref == 'W':
                lon = -lon

            return f"{lat:.6f}, {lon:.6f}"

        except Exception as e:
            # Last resort catch-all
            return None

    @staticmethod
    def _extract_location_from_piexif(exif_dict: dict) -> Optional[str]:
        """Extract GPS location from piexif data and format as 'Lat, Long'."""
        try:
            if "GPS" not in exif_dict or not exif_dict["GPS"]:
                return None

            gps_info = exif_dict["GPS"]

            # Extract latitude
            if piexif.GPSIFD.GPSLatitude not in gps_info or piexif.GPSIFD.GPSLatitudeRef not in gps_info:
                return None

            lat = ExifExtractor._convert_to_degrees_piexif(gps_info[piexif.GPSIFD.GPSLatitude])
            if lat is None:
                return None

            lat_ref = gps_info[piexif.GPSIFD.GPSLatitudeRef]
            # Handle both string and bytes
            if isinstance(lat_ref, bytes):
                lat_ref = lat_ref.decode('utf-8', errors='ignore')
            if lat_ref == 'S':
                lat = -lat

            # Extract longitude
            if piexif.GPSIFD.GPSLongitude not in gps_info or piexif.GPSIFD.GPSLongitudeRef not in gps_info:
                return None

            lon = ExifExtractor._convert_to_degrees_piexif(gps_info[piexif.GPSIFD.GPSLongitude])
            if lon is None:
                return None

            lon_ref = gps_info[piexif.GPSIFD.GPSLongitudeRef]
            # Handle both string and bytes
            if isinstance(lon_ref, bytes):
                lon_ref = lon_ref.decode('utf-8', errors='ignore')
            if lon_ref == 'W':
                lon = -lon

            return f"{lat:.6f}, {lon:.6f}"

        except Exception:
            return None

    @staticmethod
    def _convert_to_degrees(value) -> Optional[float]:
        """Convert GPS coordinates to degrees from Pillow format."""
        try:
            # Pillow returns tuples of floats or IFDRational objects
            if not isinstance(value, (list, tuple)) or len(value) < 3:
                return None

            d, m, s = value[0], value[1], value[2]

            # Handle both regular floats and IFDRational objects (tuples)
            d_val = float(d) if not isinstance(d, tuple) else (float(d[0]) / float(d[1]) if d[1] != 0 else 0)
            m_val = float(m) if not isinstance(m, tuple) else (float(m[0]) / float(m[1]) if m[1] != 0 else 0)
            s_val = float(s) if not isinstance(s, tuple) else (float(s[0]) / float(s[1]) if s[1] != 0 else 0)

            return d_val + m_val / 60.0 + s_val / 3600.0
        except (TypeError, ValueError, ZeroDivisionError, IndexError):
            return None

    @staticmethod
    def _convert_to_degrees_piexif(value) -> Optional[float]:
        """Convert GPS coordinates to degrees from piexif format (tuples of rationals)."""
        try:
            if not isinstance(value, (list, tuple)) or len(value) < 3:
                return None

            d, m, s = value[0], value[1], value[2]

            # piexif returns tuples of (numerator, denominator)
            if not all(isinstance(x, tuple) and len(x) == 2 for x in [d, m, s]):
                return None

            d_val = d[0] / d[1] if d[1] != 0 else 0
            m_val = m[0] / m[1] if m[1] != 0 else 0
            s_val = s[0] / s[1] if s[1] != 0 else 0

            return d_val + m_val / 60.0 + s_val / 3600.0
        except (TypeError, ValueError, ZeroDivisionError, IndexError):
            return None

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
            exif_success_count = 0
            exif_missing_count = 0
            sample_debug_messages = []  # Store first few for logging

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
                        created_at, location, debug_msg = future.result()
                        metadata_list.append((img_path, created_at, location))

                        # Track EXIF extraction success
                        if location and location != "Unknown Location":
                            exif_success_count += 1
                        else:
                            exif_missing_count += 1
                            # Log first 3 failed extractions for debugging
                            if len(sample_debug_messages) < 3:
                                sample_debug_messages.append(f"{os.path.basename(img_path)}: {debug_msg}")

                        self.progress_update.emit(i + 1, total_files)
                    except Exception as e:
                        self.log_message.emit(f"Error reading {os.path.basename(img_path)}: {str(e)}")
                        errors += 1

            # Log EXIF extraction summary
            self.log_message.emit(f"EXIF data found: {exif_success_count} images, Missing: {exif_missing_count} images")

            # Log sample debug messages to help diagnose issues
            if sample_debug_messages:
                self.log_message.emit("Sample EXIF extraction details:")
                for msg in sample_debug_messages:
                    self.log_message.emit(f"  {msg}")

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
