"""
GPS Diagnostic Script - Shows exact GPS data format in images
"""
from PIL import Image
import piexif
import sys

def diagnose_gps(image_path):
    print(f"\n{'='*60}")
    print(f"Analyzing: {image_path}")
    print(f"{'='*60}")

    # Method 1: Pillow getexif()
    print("\n--- PILLOW METHOD ---")
    try:
        with Image.open(image_path) as img:
            exif_data = img.getexif()

            if 34853 in exif_data:
                print("[OK] GPS tag (34853) exists")
                try:
                    gps_info = exif_data.get_ifd(34853)
                    print(f"[OK] GPS IFD accessed, {len(gps_info)} tags found")
                    print(f"  Available GPS tags: {list(gps_info.keys())}")

                    for tag_id in [1, 2, 3, 4]:
                        if tag_id in gps_info:
                            tag_names = {1: "GPSLatitudeRef", 2: "GPSLatitude",
                                       3: "GPSLongitudeRef", 4: "GPSLongitude"}
                            value = gps_info[tag_id]
                            print(f"  Tag {tag_id} ({tag_names.get(tag_id, 'Unknown')}): {value}")
                            print(f"    Type: {type(value)}")
                            if isinstance(value, (tuple, list)):
                                print(f"    Length: {len(value)}")
                                for i, item in enumerate(value):
                                    print(f"      [{i}]: {item} (type: {type(item)})")
                except Exception as e:
                    print(f"[FAIL] Failed to access GPS IFD: {type(e).__name__}: {e}")
            else:
                print("[FAIL] No GPS tag (34853)")
    except Exception as e:
        print(f"[FAIL] Pillow error: {type(e).__name__}: {e}")

    # Method 2: piexif
    print("\n--- PIEXIF METHOD ---")
    try:
        exif_dict = piexif.load(image_path)

        if "GPS" in exif_dict and exif_dict["GPS"]:
            gps_info = exif_dict["GPS"]
            print(f"[OK] GPS IFD found, {len(gps_info)} tags")
            print(f"  Available GPS tags: {list(gps_info.keys())}")

            # piexif uses numeric constants
            for tag_id in [piexif.GPSIFD.GPSLatitudeRef, piexif.GPSIFD.GPSLatitude,
                          piexif.GPSIFD.GPSLongitudeRef, piexif.GPSIFD.GPSLongitude]:
                if tag_id in gps_info:
                    tag_names = {1: "GPSLatitudeRef", 2: "GPSLatitude",
                               3: "GPSLongitudeRef", 4: "GPSLongitude"}
                    value = gps_info[tag_id]
                    print(f"  Tag {tag_id} ({tag_names.get(tag_id, 'Unknown')}): {value}")
                    print(f"    Type: {type(value)}")
                    if isinstance(value, (tuple, list)):
                        print(f"    Length: {len(value)}")
                        for i, item in enumerate(value):
                            print(f"      [{i}]: {item} (type: {type(item)})")
        else:
            print("[FAIL] No GPS IFD")
    except Exception as e:
        print(f"[FAIL] piexif error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    # Test with the first image that has GPS data
    test_image = r"C:/Users/newco/Desktop/Liz phone 25-Jul-24/PXL_20240302_182502755.MP~2.jpg"

    if len(sys.argv) > 1:
        test_image = sys.argv[1]

    diagnose_gps(test_image)
