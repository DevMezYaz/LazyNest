@echo off
pip install PyQt6 ezdxf shapely pyclipper numpy openpyxl pyinstaller Pillow

python -c "from PIL import Image; img=Image.open('icon.png'); img.save('icon.ico', format='ICO', sizes=[(256,256),(128,128),(64,64),(32,32),(16,16)])" 2>nul

python -m PyInstaller --onefile --windowed --name LazyNest ^
  --icon icon.ico ^
  --add-data "icon.png;." ^
  --hidden-import ezdxf ^
  --hidden-import ezdxf.fonts ^
  --hidden-import shapely ^
  --hidden-import shapely.geometry ^
  --hidden-import shapely.affinity ^
  --hidden-import shapely.ops ^
  --hidden-import openpyxl ^
  --hidden-import numpy ^
  --hidden-import pyclipper ^
  --hidden-import PIL ^
  --collect-all PyQt6 ^
  --collect-all ezdxf ^
  --collect-all shapely ^
  app.py

echo.
echo Done. File is at dist\LazyNest.exe
pause