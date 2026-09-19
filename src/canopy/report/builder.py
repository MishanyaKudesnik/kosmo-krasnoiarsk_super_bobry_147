"""The saved report.

One template produces the printable report the case asks for: territory and
period, results, map, sources with dates, method, parameters, assumptions,
uncertainty, and the identifier and date of the calculation.  It is built from
the same result payload the interface renders, so the two can never disagree --
a requirement the case states explicitly ("the information presented must agree
with the report").

The HTML is self-contained: no external stylesheet, no font, no script.  It
prints to PDF from any browser and opens with no network.
"""

from __future__ import annotations

import html
import json
from typing import Any, Dict, List, Optional

CSS = """
:root{--ink:#14201a;--muted:#5d6b63;--line:#d9e2dc;--accent:#1f6f4a;--warn:#8a5a00;--bad:#9b2c2c}
*{box-sizing:border-box}
body{margin:0;padding:32px;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:#fff;max-width:1000px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:14px;margin:18px 0 6px}
.sub{color:var(--muted);margin:0 0 18px}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13px}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{background:#f3f7f4;font-weight:600}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.kpi{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0}
.kpi div{border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:150px}
.kpi b{display:block;font-size:19px;font-variant-numeric:tabular-nums}
.kpi span{color:var(--muted);font-size:12px}
.tag{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;border:1px solid var(--line);background:#f3f7f4}
.warn{color:var(--warn)}.bad{color:var(--bad)}.ok{color:var(--accent)}
.note{background:#f7faf8;border-left:3px solid var(--accent);padding:8px 12px;margin:10px 0;font-size:13px}
.alarm{background:#fdf6ec;border-left:3px solid var(--warn)}
code{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;background:#f3f7f4;padding:1px 4px;border-radius:3px}
.small{font-size:12px;color:var(--muted)}
@media print{body{padding:0}h2{page-break-after:avoid}table{page-break-inside:avoid}}
"""


def _n(value: Optional[float], digits: int = 2, dash: str = "—") -> str:
    if value is None:
        return dash
    try:
        s = f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return dash
    # narrow space between thousands, comma before the decimals (ru usage)
    return s.replace(",", " ").replace(".", ",")


def _q(quantity: Optional[Dict[str, Any]], digits: int = 2) -> str:
    if not quantity:
        return "—"
    if quantity.get("value") is None:
        code = (quantity.get("status") or {}).get("code", "")
        msg = (quantity.get("status") or {}).get("message", "")
        return f'<span class="warn">{html.escape(code)}</span><br><span class="small">{html.escape(msg)}</span>'
    return f'{_n(quantity["value"], digits)} <span class="small">{html.escape(quantity.get("unit",""))}</span>'


def build_html_report(result: Dict[str, Any]) -> str:
    req = result.get("request", {})
    geo = result.get("geometry", {})
    carbon = result.get("carbon", {})
    change = result.get("change", {})
    uncert = result.get("uncertainty", {})
    base = result.get("baseline", {})
    units = result.get("units", {})
    evidence = result.get("evidence", {})
    readiness = result.get("readiness", {})

    parts: List[str] = []
    a = parts.append
    a(f"<!doctype html><html lang='ru'><head><meta charset='utf-8'>")
    a(f"<title>FOREST BEAVER — отчёт {html.escape(result.get('analysis_id',''))}</title>")
    a(f"<style>{CSS}</style></head><body>")

    a("<h1>Отчёт о спутниковой проверке углеродного результата</h1>")
    a(
        f"<p class='sub'>FOREST BEAVER · идентификатор расчёта <code>{html.escape(result.get('analysis_id',''))}</code> · "
        f"дата расчёта {html.escape(result.get('created_at',''))}</p>"
    )

    if readiness.get("uses_fixtures"):
        a(
            "<div class='note alarm'><b>Внимание: использованы синтетические растры.</b> "
            "Значения пикселей в наборе <code>data_fixtures/</code> сгенерированы для "
            "проверки конвейера. Сетки, CRS, типы данных, геометрия участков, "
            "опубликованные средние запасы 2015 и 2019 годов, доли классов SCL и окно "
            "пожара взяты из материалов кейса. Поместите настоящий архив в "
            "<code>data/</code> — каталог начнёт использовать его автоматически.</div>"
        )

    # -- territory and period ---------------------------------------------
    a("<h2>Территория и период</h2>")
    ag = geo.get("analysis_grid", {})
    a("<table>")
    a(f"<tr><th>Период</th><td>{req.get('year_start')}–{req.get('year_end')} "
      f"(Δt = {carbon.get('delta_years')} лет)</td></tr>")
    a(f"<tr><th>Площадь запроса</th><td class='num'>{_n(geo.get('area_ha'), 4)} га "
      f"({_n(geo.get('area_km2'), 4)} км²)</td></tr>")
    a(f"<tr><th>Метод площади</th><td>Геодезическая площадь на эллипсоиде WGS 84 "
      f"(аутальная сфера). Воспроизводит <code>areas.csv</code> с точностью 1e-6.</td></tr>")
    a(f"<tr><th>Сетка анализа</th><td>{html.escape(str(ag.get('crs','')))}, шаг "
      f"{ag.get('pixel_deg', 0):.9f}°, пиксель {_n(ag.get('pixel_m',[0,0])[0],1)}×"
      f"{_n(ag.get('pixel_m',[0,0])[1],1)} м = <b>{_n(ag.get('pixel_area_ha'),4)} га</b>, "
      f"а не 1 га (допущение «1 пиксель = 1 га» завысило бы результат в "
      f"{_n(ag.get('naive_1ha_overestimate_factor'),3)} раза)</td></tr>")
    w = geo.get("weights", {})
    a(f"<tr><th>Пиксели контура</th><td>полных {w.get('full_pixels')}, краевых "
      f"{w.get('partial_pixels')}; доля площади в краевых "
      f"{_n((w.get('partial_area_share') or 0)*100,1)} %</td></tr>")
    a("</table>")

    # -- results -----------------------------------------------------------
    a("<h2>Результат по учитываемому пулу</h2>")
    a(f"<p class='small'>Учитываемый пул: <b>{html.escape(carbon.get('pool',''))}</b>. "
      f"Знак: {html.escape(carbon.get('sign_convention',''))}. "
      f"CF = {carbon.get('cf')} т C/т с.в.; CO₂-экв. = ΔC × 44/12. "
      "Корни, мёртвая древесина, подстилка, почва и древесная продукция в расчёт не входят; "
      "это не означает, что их запасы равны нулю.</p>")
    ss, se = carbon.get("state_start") or {}, carbon.get("state_end") or {}
    a("<div class='kpi'>")
    a(f"<div><b>{_n(ss.get('mean_tc_ha'),3)}</b><span>запас {req.get('year_start')}, т C/га</span></div>")
    a(f"<div><b>{_n(se.get('mean_tc_ha'),3)}</b><span>запас {req.get('year_end')}, т C/га</span></div>")
    a(f"<div><b>{_n(carbon.get('delta_c_tc'),1)}</b><span>ΔC, т C</span></div>")
    a(f"<div><b>{_q(carbon.get('E'),1)}</b><span>E за период</span></div>")
    a(f"<div><b>{_q(carbon.get('e_per_ha_year'),4)}</b><span>e</span></div>")
    a("</div>")
    if (carbon.get("coverage_fraction") or 0) < 1 - 1e-9:
        a(f"<div class='note alarm'>Неполное покрытие: рассчитано "
          f"{_n((carbon.get('coverage_fraction') or 0)*100,2)} % площади, пропуски "
          f"{_n(carbon.get('area_gap_ha'),2)} га. По правилу кейса потенциальные "
          f"единицы для такого запроса не рассчитываются.</div>")

    series = carbon.get("annual_series") or []
    if series:
        a("<h3>Годовая динамика</h3><table><tr><th>Год</th><th class='num'>Средний запас, т C/га</th>"
          "<th class='num'>Суммарный запас, т C</th></tr>")
        for row in series:
            a(f"<tr><td>{row['year']}</td><td class='num'>{_n(row['mean_tc_ha'],3)}</td>"
              f"<td class='num'>{_n(row['total_tc'],1)}</td></tr>")
        a("</table>")
        a("<p class='small'>Годовой ряд показан для наглядности. Итог считается только "
          "по состояниям на границах периода: суммирование годовых разностей с "
          "накопленным результатом дало бы двойной учёт.</p>")

    # -- change ------------------------------------------------------------
    a("<h2>Изменения и их основания</h2>")
    a(f"<p class='small'>{html.escape(change.get('threshold_note',''))}</p>")
    patches = change.get("patches") or []
    if patches:
        a("<table><tr><th>Очаг</th><th class='num'>Площадь, га</th>"
          "<th class='num'>Вклад, т CO₂-экв.</th><th class='num'>Доля</th>"
          "<th>Окно дат</th><th>Сужение дал</th><th>Статус причины</th></tr>")
        for p in patches:
            fw = p.get("final_window") or {}
            a(
                f"<tr><td>{html.escape(p['patch_id'])}</td>"
                f"<td class='num'>{_n(p['area_ha'],1)}</td>"
                f"<td class='num'>{_n(p['e_tco2e'],0)}</td>"
                f"<td class='num'>{_n((p.get('contribution_share') or 0)*100,1)} %</td>"
                f"<td>{html.escape(str(fw.get('start')))} … {html.escape(str(fw.get('end')))} "
                f"<span class='small'>({fw.get('days')} дн.)</span></td>"
                f"<td>{html.escape(str(p.get('narrowing_source') or '—'))}</td>"
                f"<td><span class='tag'>{html.escape(p.get('rung',''))}</span> "
                f"{html.escape(p.get('cause',''))}</td></tr>"
            )
        a("</table>")
        a(f"<p class='small'>Сумма вкладов очагов плюс диффузное изменение "
          f"({_n(change.get('diffuse_e_tco2e'),0)} т CO₂-экв., "
          f"{_n((change.get('diffuse_share') or 0)*100,1)} % итога) в точности равна E. "
          "Диффузная часть — изменения ниже порога выделения очага; они не отбрасываются.</p>")
    else:
        a("<p>Значимых очагов изменений не выделено.</p>")

    # -- uncertainty -------------------------------------------------------
    a("<h2>Неопределённость</h2>")
    a(f"<p class='small'>{html.escape(uncert.get('interval_kind_note',''))}</p>")
    a("<div class='kpi'>")
    a(f"<div><b>{_q(uncert.get('L'),0)}</b><span>L</span></div>")
    a(f"<div><b>{_q(uncert.get('U'),0)}</b><span>U</span></div>")
    a(f"<div><b>{_q(uncert.get('H'),0)}</b><span>H</span></div>")
    a("</div>")
    if uncert.get("scenarios"):
        a("<h3>Сценарии модели ошибки</h3><table><tr><th>Сценарий</th>"
          "<th>Пространственная модель</th><th class='num'>ρ_t</th>"
          "<th class='num'>σ(E)</th><th class='num'>H</th></tr>")
        for s in uncert["scenarios"]:
            mark = " ←" if s["id"] == uncert.get("active_scenario") else ""
            a(f"<tr><td>{html.escape(s['label'])}{mark}</td>"
              f"<td>{html.escape(s['spatial_model'])}"
              + (f", L={_n(s.get('correlation_length_m'),0)} м" if s.get('correlation_length_m') else "")
              + f"</td><td class='num'>{_n(s['temporal_correlation'],2)}</td>"
              f"<td class='num'>{_n(s['sigma_e'],0)}</td>"
              f"<td class='num'>{_n(s['H'],0)}</td></tr>")
        a("</table>")
    if uncert.get("cross_check"):
        cc = uncert["cross_check"]
        a(f"<div class='note'>Сверка с собственным продуктом разности ESA за 2019→2020: "
          f"наша σ(ΔC) = {_n(cc.get('our_sigma_delta_c_tc'),1)} т C, ESA — "
          f"{_n(cc.get('esa_sigma_delta_c_tc'),1)} т C. Согласованное ρ_t = "
          f"{_n(cc.get('implied_rho_t'),3)}. {html.escape(cc.get('note',''))}</div>")
    a("<h3>Слои</h3><table><tr><th>Слой</th><th>Показатель</th><th class='num'>Оценка</th>"
      "<th>Влияет на L и U</th></tr>")
    for layer in uncert.get("layers") or []:
        a(f"<tr><td>{html.escape(layer['title'])}</td><td>{html.escape(layer['headline'])}</td>"
          f"<td class='num'>{_n(layer['score'],2)}</td>"
          f"<td>{'да' if layer.get('affects_interval') else 'нет'}</td></tr>")
    a("</table>")
    if uncert.get("vri") is not None:
        a(f"<p class='small'>Индекс готовности к верификации VRI = {_n(uncert['vri'],2)}. "
          f"{html.escape(uncert.get('vri_note',''))}</p>")

    # -- baseline and units ------------------------------------------------
    a("<h2>Базовая линия и потенциальные единицы</h2>")
    if base.get("parts"):
        a("<table><tr><th>Участок</th><th class='num'>Площадь запроса, га</th>"
          "<th class='num'>Запас начало, т C/га</th><th class='num'>Запас конец, т C/га</th>"
          "<th class='num'>E_base, т CO₂-экв.</th></tr>")
        for p in base["parts"]:
            a(f"<tr><td>{html.escape(p['aoi_id'])}</td><td class='num'>{_n(p['area_ha'],2)}</td>"
              f"<td class='num'>{_n(p['stock_start_tc_ha'],4)}</td>"
              f"<td class='num'>{_n(p['stock_end_tc_ha'],4)}</td>"
              f"<td class='num'>{_n(p['e_base_tco2e'],1)}</td></tr>")
        a("</table>")
    a("<table>")
    for key, label, digits in (
        ("R", "R = E_base − E_proj − LK", 1),
        ("H", "H", 1),
        ("H_over_R", "H/R", 4),
        ("UNC", "UNC = max(0; H/R − 0,10)", 4),
        ("R_adj", "R_adj = R × (1 − UNC)", 1),
        ("B", "B = 0,15 × R_adj", 1),
        ("Q", "Q = ⌊R_adj − B⌋", 0),
    ):
        a(f"<tr><th>{html.escape(label)}</th><td class='num'>{_q(units.get(key), digits)}</td></tr>")
    a("</table>")
    a(f"<div class='note'><b>{html.escape(units.get('disclaimer',''))}</b></div>")
    if units.get("value_scenarios"):
        a("<table><tr><th>Ценовой сценарий</th><th class='num'>Цена</th><th class='num'>V = Q × p</th></tr>")
        for v in units["value_scenarios"]:
            a(f"<tr><td>{html.escape(str(v['label']))}</td><td class='num'>{_n(v['price'],0)}</td>"
              f"<td class='num'>{_n(v['value'],0)}</td></tr>")
        a("</table>")
    scen = result.get("baseline_scenarios") or []
    if scen:
        a("<h3>Стресс-тест допущений</h3><table><tr><th>Вариант</th>"
          "<th class='num'>Q</th><th>Статус</th><th>Зачётный</th></tr>")
        for s in scen:
            a(f"<tr><td>{html.escape(s['label'])}</td>"
              f"<td class='num'>{_n((s.get('Q') or {}).get('value'),0)}</td>"
              f"<td class='small'>{html.escape((s.get('status') or {}).get('code',''))}</td>"
              f"<td>{'да' if s.get('counts_for_units') else 'нет — исследовательский'}</td></tr>")
        a("</table>")

    # -- sources -----------------------------------------------------------
    a("<h2>Источники, версии и условия использования</h2>")
    a("<table><tr><th>Источник</th><th>Версия</th><th>DOI / ссылка</th>"
      "<th>Атрибуция</th><th>Ограничения</th></tr>")
    for s in evidence.get("sources") or []:
        link = s.get("doi") or s.get("url") or ""
        a(f"<tr><td>{html.escape(str(s.get('product','')))}<br><span class='small'>"
          f"{html.escape(str(s.get('source_id','')))}</span></td>"
          f"<td>{html.escape(str(s.get('version','')))}</td>"
          f"<td class='small'>{html.escape(str(link))}</td>"
          f"<td class='small'>{html.escape(str(s.get('attribution','')))}</td>"
          f"<td class='small'>{html.escape(str(s.get('limitations','')))}</td></tr>")
    a("</table>")

    a("<h2>Метод, параметры и допущения</h2>")
    a("<ul>")
    for line in uncert.get("assumptions") or []:
        a(f"<li>{html.escape(line)}</li>")
    a("<li>Метод разности запасов (stock-difference), МГЭИК 2006, том 4, глава 2.</li>")
    a("<li>Сравнение спутниковых продуктов между собой не является независимой "
      "наземной валидацией: наземных измерений в наборе нет.</li>")
    a("<li>Причина нарушения указывается только при наличии подтверждений; иначе "
      "сохраняется статус «причина не установлена».</li>")
    a("</ul>")

    # -- reproduction ------------------------------------------------------
    man = result.get("manifest", {})
    a("<h2>Воспроизведение расчёта</h2>")
    a(f"<p>Команда: <code>{html.escape(str(man.get('replay','')))}</code>. "
      f"Версия кода: <code>{html.escape(str(man.get('code_version','')))}</code>.</p>")
    inputs = man.get("inputs") or []
    a(f"<p class='small'>Входных файлов: {len(inputs)}. Полный список с sha256 — в "
      f"<code>manifest.json</code>.</p>")
    a("<table><tr><th>Файл</th><th>Продукт</th><th>Происхождение</th><th class='small'>sha256</th></tr>")
    for item in inputs[:12]:
        a(f"<tr><td class='small'>{html.escape(item['file'])}</td>"
          f"<td class='small'>{html.escape(item['product'])}</td>"
          f"<td class='small'>{html.escape(item['origin'])}</td>"
          f"<td class='small'>{html.escape(item['sha256'][:16])}…</td></tr>")
    if len(inputs) > 12:
        a(f"<tr><td colspan='4' class='small'>… и ещё {len(inputs)-12} файлов</td></tr>")
    a("</table>")

    a("</body></html>")
    return "".join(parts)
