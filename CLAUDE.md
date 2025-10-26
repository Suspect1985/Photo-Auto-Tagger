# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Auto-Tagger** is a standalone PyQt6 desktop application that recursively scans photo folders and automatically creates date and location tags based on EXIF metadata. It creates a normalized SQLite database (`photo_library.db`) in the user's selected folder.

## Key Commands

### Development
```bash
# Install dependencies
pip install -r requirements.txt

# Run application
python autotagger_app.py
```

### Building Executable
```bash
# Build portable .exe (output to dist/)
pyinstaller --onefile --windowed autotagger_app.py
```

## Architecture

### Single-File Monolithic Design
The application is intentionally built as a single file (`autotagger_app.py`) for portability and simple PyInstaller bundling. All components are in one module with clear section separators.

### Threading Architecture
- **Main Thread**: PyQt6 event loop and UI rendering
- **Worker Thread**: `AutoTaggerWorker` (QThread) handles all blocking operations (file I/O, database operations)
- **Thread Pool**: `ThreadPoolExecutor` (4 workers) for parallel EXIF extraction
- **Communication**: PyQt signals/slots for thread-safe UI updates (`progress_update`, `phase_update`, `log_message`, `finished`)

### Core Components (in order of dependency)

1. **DatabaseManager** - SQLite operations with WAL mode
   - Creates normalized schema: PhotoMetadata, Tags, Photo_Tags_Link
   - Implements batch transactions via `begin_transaction()`/`commit()`
   - Uses `upsert_photo()` for idempotent operations (re-running is safe)
   - Tag cache pattern in worker to minimize duplicate lookups

2. **ExifExtractor** - Static methods for metadata extraction
   - Tries EXIF tags: 36867 (DateTimeOriginal), 36868 (DateTimeDigitized), 306 (DateTime)
   - GPS extraction from EXIF tag 34853, converts DMS to decimal degrees
   - Fallback to filesystem mtime if EXIF unavailable
   - Returns tuple: `(creation_date_str, location_str)` or `(date, None)`

3. **AutoTaggerWorker** - QThread performing 3-phase process:
   - Phase 1: Scan filesystem recursively for supported extensions (see `SUPPORTED_EXTENSIONS`)
   - Phase 2: Parallel EXIF extraction using ThreadPoolExecutor
   - Phase 3: Batch database inserts with transactions for photos and tags
   - Emits progress updates after each file/batch for UI responsiveness

4. **AutoTaggerWindow** - Main PyQt6 window
   - Manages worker lifecycle (start/cancel)
   - Connects worker signals to UI update slots
   - Database path: `{selected_folder}/photo_library.db`

### Database Schema Critical Details

- **PhotoMetadata.image_path** is UNIQUE - upserts prevent duplicates
- **Tags.name** is UNIQUE - normalized tags (one tag per year, one per location)
- **Photo_Tags_Link** has UNIQUE(photo_id, tag_id) - prevents duplicate tagging
- **Performance**: WAL journal mode, NORMAL synchronous, indexed junction table
- Each photo gets exactly 2 tags: year (e.g., "2021") and location (e.g., "37.7749, -122.4194" or "Unknown Location")

### Image Format Support

Multi-format via Pillow: `.jpg`, `.jpeg`, `.png`, `.webp`, `.heic`, `.tiff`, `.tif`, `.bmp`, `.gif`

To add formats, modify `SUPPORTED_EXTENSIONS` constant (line 28).

## Common Development Patterns

### Adding New EXIF Fields
If adding rotation, camera model, etc.:
1. Extract in `ExifExtractor.extract_metadata()` - return as additional tuple element
2. Update `DatabaseManager.upsert_photo()` to accept new parameter
3. Update `AutoTaggerWorker.run()` Phase 2 to pass new data
4. Modify database schema in `DatabaseManager._create_schema()`

### Adding New Tag Types
Current: year tags + location tags. To add (e.g., camera model tags):
1. Extract metadata in `ExifExtractor`
2. In `AutoTaggerWorker.run()` Phase 3, add to `photo_tags` tuple
3. Create tag via `db.get_or_create_tag()` with cache pattern
4. Link via `db.link_photo_tag(photo_id, new_tag_id)`

### Modifying Thread Pool Size
Line 310: `ThreadPoolExecutor(max_workers=4)` - adjust for CPU cores or I/O characteristics.

## Important Constraints

- **No file modification**: Application only reads images and writes database
- **Idempotent by design**: Re-running on same folder updates existing entries via UPSERT
- **Single file requirement**: Keep monolithic for PyInstaller simplicity
- **Thread safety**: All database operations in worker thread (check_same_thread=False on connection)
- **Error tolerance**: Worker catches exceptions per file and continues processing

## Testing Notes

To test the application:
1. Create test folder with images (JPG, PNG, WEBP, etc.)
2. Some images should have EXIF dates and GPS (use smartphone photos)
3. Some images without EXIF (use screenshots)
4. Verify `photo_library.db` creation
5. Re-run on same folder to verify no duplicate tags created
6. Check for graceful handling of corrupt files

To inspect database:
```bash
sqlite3 photo_library.db
.schema
SELECT * FROM Tags;
SELECT * FROM Photo_Tags_Link;
```
