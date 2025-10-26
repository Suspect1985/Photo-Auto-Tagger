# Auto-Tagger

A high-performance standalone desktop application that automatically assigns date and location-based tags to image files using EXIF metadata.

## Features

- **Multi-format support**: JPG, JPEG, PNG, WEBP, HEIC, TIFF, BMP, GIF
- **Automatic date tagging**: Tags images by year from EXIF or filesystem timestamps
- **Automatic location tagging**: Extracts GPS coordinates from EXIF data
- **SQLite database**: Creates normalized database (`photo_library.db`) in selected folder
- **Multi-threaded processing**: Fast parallel EXIF extraction
- **Non-blocking UI**: Responsive interface with real-time progress updates
- **Error handling**: Gracefully skips corrupt or unreadable files

## Installation

### Requirements

- Python 3.8 or higher
- pip package manager

### Install Dependencies

```bash
pip install -r requirements.txt
```

## Usage

### Running the Application

```bash
python autotagger_app.py
```

### Using Auto-Tagger

1. Click **Browse** to select a folder containing your photos
2. Click **Start Auto-Tag** to begin the tagging process
3. Monitor progress in the log window
4. A `photo_library.db` SQLite database will be created in the selected folder

### Database Schema

The application creates three tables:

**PhotoMetadata**
- `id`: Primary key
- `image_path`: Unique absolute path to image
- `file_name`: Image filename
- `created_at`: ISO format creation timestamp
- `location`: GPS coordinates or "Unknown Location"
- `rotation`: Image rotation (default 0)

**Tags**
- `id`: Primary key
- `name`: Unique tag name (year or location)

**Photo_Tags_Link**
- Junction table linking photos to tags
- Prevents duplicate tags

## Building Standalone Executable

### Build .exe with PyInstaller

```bash
pyinstaller --onefile --windowed autotagger_app.py
```

The executable will be created in the `dist/` folder.

### Distribution

The generated `.exe` is fully portable and includes all dependencies. No Python installation required on target machines.

## Performance Optimizations

- **WAL journal mode**: Faster concurrent database access
- **Batch transactions**: Groups database operations for speed
- **ThreadPoolExecutor**: Parallel EXIF extraction (4 workers)
- **QThread workers**: Non-blocking UI during processing
- **Database indexes**: Fast tag and photo lookups

## Technical Details

- **Framework**: PyQt6
- **Database**: SQLite3
- **EXIF extraction**: Pillow + piexif
- **Threading**: concurrent.futures + QThread

## Notes

- Re-running Auto-Tagger on the same folder is safe (no duplicate tags)
- Original image files are never modified
- Database is created in the same folder as your photos
- GPS coordinates are formatted as "Latitude, Longitude"
- If no EXIF date exists, filesystem modification time is used

## Project Structure

```
Photo-Auto-Tagger/
├── autotagger_app.py      # Main application
├── requirements.txt       # Python dependencies
├── README.md             # This file
└── assets/
    └── icons/            # UI icons (optional)
```

## License

This software is provided as-is for photo library management.

## Support

For issues or feature requests, refer to the application log output for debugging information.
