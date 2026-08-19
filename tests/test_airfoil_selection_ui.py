"""Airfoil Sets UI contract: a dedicated gate, not Findings DOM surgery."""
from pathlib import Path


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src" / "cockpit" / "static"
JS = STATIC / "stage1-airfoils.js"
CSS = STATIC / "stage1-airfoils.css"
HTML = STATIC / "stage1.html"
STAGE1_JS = STATIC / "stage1.js"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing dedicated Airfoil Sets asset: {path.name}"
    return path.read_text(encoding="utf-8")


def test_dedicated_module_owns_all_four_api_states_and_public_navigation_event():
    source = _read(JS)
    for endpoint in (
        "/api/stage1/airfoils/recommend",
        "/api/stage1/airfoils?session=",
        "/api/stage1/airfoils/detail",
        "/api/stage1/airfoils/select",
    ):
        assert endpoint in source
    assert "export const AIRFOIL_CONTINUE_EVENT" in source
    assert "astra:stage1-airfoil-continued" in source
    assert "export function createAirfoilSetsController" in source


def test_three_strategy_cards_have_exact_station_rows_and_one_continue_action():
    source = _read(JS)
    for strategy in ("balanced", "efficiency", "low_speed_control"):
        assert strategy in source
    for station in ("root", "mid", "tip"):
        assert f"'{station}'" in source or f'"{station}"' in source
    assert "airfoil-card" in source
    html = _read(HTML)
    assert html.count('id="btn-airfoil-continue"') == 1
    assert 'id="airfoil-cards"' in html


def test_station_modal_has_outline_separate_lift_and_drag_graphs_and_evidence():
    html = _read(HTML)
    assert 'id="airfoil-modal"' in html and 'role="dialog"' in html
    for element_id in (
        "airfoil-outline", "airfoil-lift-graph", "airfoil-clmax",
        "airfoil-drag-graph", "airfoil-detail-metrics", "airfoil-evidence",
        "btn-airfoil-modal-close",
    ):
        assert f'id="{element_id}"' in html
    source = _read(JS)
    assert "prestall_lift" in source
    assert "cd_vs_cl" in source
    assert "coordinate_sha256" in source
    assert "references" in source
    assert "Evidence & References" in source
    assert "role=\"tab\"" in source
    assert "role=\"tabpanel\"" in source
    assert "aria-selected" in source


def test_stale_cards_remain_visible_while_continue_is_disabled_and_recompute_exists():
    source = _read(JS)
    assert "airfoil-stale" in source
    assert "btn-airfoil-recompute" in source
    assert "recommendation.stale" in source
    assert "btn-airfoil-continue" in source
    assert ".disabled" in source
    assert "cards" in source.lower()


def test_reload_back_keyboard_loading_and_errors_are_explicit():
    source = _read(JS)
    assert "localStorage" in source
    assert "popstate" in source
    for key in ("Escape", "ArrowLeft", "ArrowRight"):
        assert key in source
    assert "airfoil-loading" in source
    assert "airfoil-error" in source
    assert "aria-busy" in source


def test_selected_coordinates_drive_a_threejs_loft_preview():
    source = _read(JS)
    assert "THREE.BufferGeometry" in source
    assert "detail.coordinates" in source
    assert "airfoil-loft-canvas" in source
    assert "coordinate" in source.lower()


def test_concepts_preview_consumes_exact_coordinate_section_provider():
    airfoils = _read(JS)
    stage1 = _read(STAGE1_JS)
    assert "export function createAirfoilSectionProvider" in airfoils
    assert "airfoilSectionProvider" in stage1
    assert "sectionLoop(s.chord, s.tc" not in stage1


def test_airfoil_assets_are_responsive_and_do_not_touch_findings_dom():
    css = _read(CSS)
    assert "@media" in css
    assert "airfoil-card-grid" in css
    assert "#topbar" in css and "flex-wrap" in css
    source = _read(JS).lower()
    assert "getelementbyid('findings" not in source
    assert 'getelementbyid("findings' not in source
    assert "queryselector('#findings" not in source


def test_shared_shell_only_mounts_registers_and_navigates_the_new_screen():
    html = _read(HTML)
    stage1 = _read(STAGE1_JS)
    assert 'href="stage1-airfoils.css?v=2"' in html
    assert 'src="stage1.js?v=2"' in html
    assert 'id="screen-airfoils"' in html
    assert "stage1-airfoils.js?v=2" in stage1
    assert "screen-airfoils" in stage1
    assert "createAirfoilSetsController" in stage1
    assert '<button id="btn-generate" class="primary">Recommend airfoil sets →</button>' in html


def test_requirements_section_row_hands_ownership_to_the_airfoil_gate():
    from domain_packs.bwb.intake import default_intake

    row = next(row for row in default_intake().rows if row.id == "airfoil")
    assert row.value == "Chosen in Airfoil sets"
    assert "Airfoil sets" in row.info
    assert row.status == "derived", "the ownership status must not say recorded only"
