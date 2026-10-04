# -*- coding: utf-8 -*-
"""Сборка устанавливаемого ZIP плагина TopoPolyEdit.

Запуск из корня репозитория:
    python tools/make_zip.py

Результат: dist/topopolyedit.zip — готов для «Установить из ZIP» в QGIS
(архив содержит верхнюю папку topopolyedit/ с metadata.txt).
"""
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "topopolyedit")
DIST = os.path.join(ROOT, "dist")
DST = os.path.join(DIST, "topopolyedit.zip")


def main():
    if not os.path.isfile(os.path.join(SRC, "metadata.txt")):
        raise SystemExit("Не найден topopolyedit/metadata.txt — запускайте из корня репо")
    os.makedirs(DIST, exist_ok=True)
    if os.path.exists(DST):
        os.remove(DST)

    count = 0
    with zipfile.ZipFile(DST, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(SRC):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in sorted(files):
                if f.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, os.path.dirname(SRC))
                zf.write(full, rel)
                count += 1

    print("ZIP:", DST)
    print("Файлов в архиве:", count)
    print("Размер, байт:", os.path.getsize(DST))
    with zipfile.ZipFile(DST) as zf:
        names = zf.namelist()
    assert all(n.startswith("topopolyedit/") for n in names), names
    assert "topopolyedit/metadata.txt" in names
    print("Структура архива: OK (верхняя папка topopolyedit/, metadata.txt на месте)")


if __name__ == "__main__":
    main()
