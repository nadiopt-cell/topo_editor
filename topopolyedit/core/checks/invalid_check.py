# -*- coding: utf-8 -*-
"""Проверка INVALID: невалидная геометрия (этап 5 ТЗ).

Точки нарушений — из QgsGeometry.validateGeometry() (метод GEOS);
устойчивое извлечение текста переиспользует правило GeosValidity
валидатора этапа 1. Исправление: вручную (показ места); опция
makeValid с превью — payload {"makevalid": True} (диалог панели
показывает результат до записи).
"""

from qgis.core import QgsPointXY

from ..validator import GeosValidity
from .base import Finding, CODE_INVALID, _cancelled, _progress


class InvalidCheck(object):
    """isGeosValid() каждой фичи; одна находка на точку ошибки GEOS."""

    CODE = CODE_INVALID

    def run(self, ctx):
        out = []
        geoms = ctx.get("geoms") or []
        total = max(len(geoms), 1)
        for i, (fid, g) in enumerate(geoms):
            if _cancelled(ctx):
                return out
            if g is None or g.isNull() or g.isEmpty():
                continue
            if g.isGeosValid():
                continue
            errs = GeosValidity._validator_errors(g)
            if not errs:
                out.append(self._finding(ctx, fid, None,
                                         u"геометрия невалидна (GEOS)"))
            else:
                for msg, pt in errs:
                    out.append(self._finding(ctx, fid, pt, msg))
            _progress(ctx, (i + 1) / float(total),
                      u"INVALID: проверка геометрий")
        return out

    @staticmethod
    def _finding(ctx, fid, pt, msg):
        return Finding(
            CODE_INVALID, fid=fid,
            point=QgsPointXY(pt) if pt is not None else None,
            area=None, message=msg or u"геометрия невалидна",
            fixable=True,  # makeValid как явный выбор (превью в диалоге)
            payload={"makevalid": True},
            layer_id=ctx.get("layer_id", u""),
            layer_name=ctx.get("layer_name", u""))
