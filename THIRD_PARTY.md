# Сторонние компоненты и данные

## Библиотеки фронтенда (`app/vendor/`)
| Компонент | Версия | Лицензия |
|---|---|---|
| MapLibre GL JS | 4.7.1 | BSD-3-Clause |
| Chart.js | 4.4.4 | MIT |
| Упрощённые контуры стран (`world-110m.geojson`) | 110m | источник в файле не указан; используется только как декоративная подложка |

Серверная часть зависит только от NumPy (BSD-3-Clause).

## Данные
Условия использования, DOI и обязательное указание авторства для ESA CCI Biomass v7.0,
Sentinel-2 L2A, Global Forest Change (Hansen et al., v1.13, CC BY 4.0) и MODIS MCD64A1
приведены в `data/sources.csv`. Sentinel-2: «Contains modified Copernicus Sentinel data 2019–2024».
Целостность файлов проверяется по SHA-256 из `data/file_catalog.csv`.

Опциональные внешние сервисы: подложка OpenStreetMap и поиск адресов Nominatim
(© OpenStreetMap contributors), каталог STAC Earth Search (Element 84).
