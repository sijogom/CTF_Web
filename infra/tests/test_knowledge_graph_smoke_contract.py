"""Public smoke probes must exercise the real knowledge-map path."""

from pathlib import Path
import json
import subprocess
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class KnowledgeGraphSmokeContractTests(unittest.TestCase):
    def test_ctfctl_smoke_probes_map_page_and_v2_overview(self) -> None:
        common = (PROJECT_ROOT / "infra" / "scripts" / "lib" / "ctfctl-common.sh").read_text(encoding="utf-8")
        readme = (PROJECT_ROOT / "infra" / "scripts" / "ctfctl-README.md").read_text(encoding="utf-8")
        smoke_body = common.split("ctfctl_smoke_test() {", 1)[1].split("ctfctl_require_root()", 1)[0]
        for endpoint in (
            '"/ctf/ko/learn?view=map"',
            '"/ctf/api/learn/knowledge-map/v2/overview"',
            '"/ctf/api/learn/knowledge-map/v2/canvas"',
            '"/ctf/api/learn/knowledge-map/v2/search?q=course&limit=1"',
        ):
            self.assertIn(endpoint, smoke_body)
        self.assertIn("/ctf/ko/learn?view=map", readme)
        self.assertIn("/ctf/api/learn/knowledge-map/v2/overview", readme)
        self.assertIn("/ctf/api/learn/knowledge-map/v2/canvas", readme)
        self.assertIn("/ctf/api/learn/knowledge-map/v2/search?q=course&limit=1", readme)
        self.assertIn('"graph_revision"', smoke_body)
        self.assertIn('"mapping_coverage"', smoke_body)
        self.assertIn('"graph_revision"[[:space:]]*:[[:space:]]*"[^"]+"', smoke_body)
        self.assertIn('"mapping_coverage"[[:space:]]*:[[:space:]]*\\{', smoke_body)
        self.assertIn('"node_counts"[[:space:]]*:[[:space:]]*\\{', smoke_body)
        self.assertIn('"edge_counts"', smoke_body)
        self.assertIn('"edge_counts"[[:space:]]*:[[:space:]]*\\{', smoke_body)
        self.assertIn('"relation_count"[[:space:]]*:[[:space:]]*[0-9]+', smoke_body)
        self.assertIn('"layout_hints"[[:space:]]*:[[:space:]]*\\{', smoke_body)
        self.assertIn('"applied_filters"[[:space:]]*:[[:space:]]*\\{', smoke_body)
        self.assertIn('"mode"[[:space:]]*:[[:space:]]*"canvas"', smoke_body)
        self.assertIn("SMOKE_GRAPH_CANVAS_CONTRACT_FAILED", smoke_body)
        self.assertIn("SMOKE_GRAPH_SEARCH_CONTRACT_FAILED", smoke_body)
        self.assertIn("/ctf/api/learn/knowledge-map/v2/focus?focus_type=", smoke_body)
        self.assertIn("SMOKE_GRAPH_FOCUS_CONTRACT_FAILED", smoke_body)
        self.assertIn("SMOKE_GRAPH_FOCUS_EXPANSION_HTTP_FAILED", smoke_body)
        self.assertIn("SMOKE_GRAPH_FOCUS_EXPANSION_CONTRACT_FAILED", smoke_body)
        self.assertIn("relation_types=tag_affinity&relation_types=tag_parent", smoke_body)
        self.assertIn("SMOKE_GRAPH_FOCUS_EXPANSION_OK", smoke_body)
        self.assertIn("SMOKE_GRAPH_FOCUS_SKIPPED", smoke_body)
        self.assertIn("SMOKE_GRAPH_CONTRACT_FAILED", smoke_body)
        self.assertIn('id="learn-map-tab"', smoke_body)
        self.assertIn('aria-selected="true"', smoke_body)
        self.assertIn('id="learn-map-panel"', smoke_body)
        self.assertIn("SMOKE_GRAPH_UI_CONTRACT_FAILED", smoke_body)
        self.assertNotIn("SMOKE_GRAPH_UI_LEGACY_COPY_FAILED", smoke_body)
        self.assertIn('"next_cursor"', smoke_body)
        self.assertIn('item["entity_id"]', smoke_body)
        self.assertIn('ctfctl_require_command "python3"', smoke_body)

    def test_smoke_requires_all_public_v2_node_types(self) -> None:
        common = (PROJECT_ROOT / "infra" / "scripts" / "lib" / "ctfctl-common.sh").read_text(encoding="utf-8")
        smoke_body = common.split("ctfctl_smoke_test() {", 1)[1].split("ctfctl_require_root()", 1)[0]
        for node_type in ("challenge", "course", "lesson", "knowledge_tag", "hint", "event", "assignment"):
            self.assertIn(f'"{node_type}"[[:space:]]*:[[:space:]]*[0-9]+', smoke_body)

    def test_v2_map_component_does_not_call_legacy_graph_endpoint(self) -> None:
        component = (PROJECT_ROOT / "frontend" / "src" / "components" / "learn" / "KnowledgeGraphMap.tsx").read_text(encoding="utf-8")
        self.assertIn('/api/learn/knowledge-map/v2/overview', component)
        self.assertIn('/api/learn/knowledge-map/v2/focus', component)
        self.assertNotIn('/api/learn/knowledge-map/v2/canvas', component)
        self.assertIn('/api/learn/knowledge-map/v2/search', component)
        self.assertNotIn('/api/learn/knowledge-graph', component)

    def test_v2_map_is_a_free_canvas_not_a_linear_or_radial_list(self) -> None:
        component = (PROJECT_ROOT / "frontend" / "src" / "components" / "learn" / "KnowledgeGraphMap.tsx").read_text(encoding="utf-8")
        layout = (PROJECT_ROOT / "frontend/src/lib/knowledge-map-layout.mjs").read_text(encoding="utf-8")
        for marker in (
            'data-map-layout="compact-canvas-v3"',
            'data-map-ui-revision="free-canvas-click-focus-v3"',
            "BackgroundVariant.Dots",
            'data-canvas-mode="obsidian-canvas"',
            'const depth = 1',
            'data-map-depth={depth}',
            'import { createKnowledgeMapPositions, fitKnowledgeMapViewport } from "@/lib/knowledge-map-layout.mjs";',
            "return createKnowledgeMapPositions(graph, {",
            "nodesDraggable",
            "panOnDrag",
            "panOnScroll",
            "zoomOnScroll",
            "zoomOnPinch",
            "POSITION_STORAGE_KEY",
            "VIEWPORT_STORAGE_KEY",
            "onNodesChange={handleNodesChange}",
            "const loadFocus = useCallback",
            "void loadFocus(focus);",
        ):
            self.assertIn(marker, component)
        for marker in ("export function createKnowledgeMapPositions(", "export function fitKnowledgeMapViewport("):
            self.assertIn(marker, layout)
        resolver = component.split("function resolveCanvasPositions(", 1)[1].split("function KnowledgeCanvasNode(", 1)[0]
        self.assertRegex(resolver, r"positionMemory\.get\(positionMemoryKey\(graph,\s*node\.id\)\)\s*\?\?\s*fallbackPositions\.get\(node\.id\)")
        drag_handler = component.split("const handleNodesChange = useCallback(", 1)[1].split("const loadFocus = useCallback", 1)[0]
        for marker in (
            "positionMemoryRef.current.set(positionMemoryKey(graph, change.id), change.position)",
            "writePositionMemory(positionMemoryRef.current)",
            "resolveCanvasPositions(graph, positionMemoryRef.current)",
            "graphToFlowEdges(graph, relationLabel, describeEdge, positions)",
            "onNodesChange(changes)",
        ):
            self.assertIn(marker, drag_handler)
        for marker in (
            "positionMemoryRef.current = readPositionMemory()",
            "window.localStorage.getItem(POSITION_STORAGE_KEY)",
            "window.localStorage.setItem(POSITION_STORAGE_KEY",
            "handleSidesForEdge(sourcePosition, targetPosition)",
            "sourceHandle: `source-${sourceSide}`",
            "targetHandle: `target-${targetSide}`",
            "fitKnowledgeMapViewport(instance.getNodes(), safeCanvasBounds(), {",
            "if (fitAllNodesInSafeArea())",
            "fitAllNodesInSafeArea(180)",
            "onMoveStart={(event) => { if (event) automaticViewportRef.current = false; }}",
            "onMoveEnd={handleViewportMoveEnd}",
            "onPointerDownCapture={onCanvasPointerDownCapture}",
            "onKeyDownCapture={onCanvasKeyDownCapture}",
            'target.closest(".react-flow__controls, .react-flow__minimap")',
        ):
            self.assertIn(marker, component)
        viewport_handler = component.split("const handleViewportMoveEnd = useCallback(", 1)[1].split("const markViewportControlIntent", 1)[0]
        self.assertIn("automaticViewportRef.current", viewport_handler.split("return;", 1)[0])
        self.assertIn("writeViewportMemory(viewportMemoryRef.current)", viewport_handler)
        self.assertIn("MiniMap", component)
        minimap = component.split("<MiniMap", 1)[1].split("/>", 1)[0]
        self.assertIn("pannable", minimap)
        self.assertIn("zoomable", minimap)
        self.assertIn('!right-4 !left-auto', component)
        self.assertNotIn('!left-4 !m-0 !rounded-lg', component)

    def test_map_tools_preserve_canvas_and_localize_accessible_controls(self) -> None:
        component = (PROJECT_ROOT / "frontend/src/components/learn/KnowledgeGraphMap.tsx").read_text(encoding="utf-8")
        tools = (PROJECT_ROOT / "frontend/src/components/learn/KnowledgeMapTools.tsx").read_text(encoding="utf-8")
        page = (PROJECT_ROOT / "frontend/src/app/[locale]/learn/page.tsx").read_text(encoding="utf-8")
        header = (PROJECT_ROOT / "frontend/src/components/layout/Header.tsx").read_text(encoding="utf-8")
        for marker in ("<KnowledgeMapTools", "mergeMapPage", 'params.set("cursor", cursor)', "graph.next_cursor", "graph.truncated", "ensureSelectedNodeVisible"):
            self.assertIn(marker, component)
        self.assertNotIn('params.set("continuation"', component)
        for marker in ('aria-expanded={open}', 'aria-controls="knowledge-map-tools"', 'type="search"', 'type="checkbox"', "onPick(item)", "onRetry"):
            self.assertIn(marker, tools)
        for marker in ('nodeOrigin={[0.5, 0.5]}', 'handleSidesForEdge', 'const commitNodeSelection', 'onFocusChange?.(', 'system_prerequisite', 'data.typeLabel', 'aria-label={t("canvasLabel")}'):
            self.assertIn(marker, component)
        self.assertNotIn("화면 읽기용 관계 목록", component)
        self.assertNotIn('data-map-controls="overlay"', component)
        for marker in ('{visibleViewMode === "list" && (', '{visibleViewMode === "map" && (', 'role="tablist"', 'id="learn-list-panel"', 'id="learn-map-panel"', 'writeUrlQuery', 'window.addEventListener("popstate"'):
            self.assertIn(marker, page)
        self.assertNotIn('hidden={visibleViewMode !== "list"}', page)
        self.assertNotIn('hidden={visibleViewMode !== "map"}', page)
        self.assertNotIn('"classes"', header.split("const NAV_KEYS", 1)[1].split(";", 1)[0])
        required = ("searchAndFilter", "search", "contentTypes", "relations", "truncated", "loadMore", "requiredCondition", "optionalRelation", "retry")
        for locale in ("ko", "en", "ja", "zh"):
            messages = json.loads((PROJECT_ROOT / "frontend/messages" / f"{locale}.json").read_text(encoding="utf-8"))
            for key in required:
                self.assertTrue(messages["knowledgeMap"][key], (locale, key))
            self.assertNotIn("classes", messages["nav"])

    def test_direct_map_entry_can_return_to_list_and_refetch_courses(self) -> None:
        """A pushState view change must not leave the list with stale route state."""
        learn_page = (PROJECT_ROOT / "frontend" / "src" / "app" / "[locale]" / "learn" / "page.tsx").read_text(
            encoding="utf-8"
        )
        fetch_effect = learn_page.split("useEffect(() => {", 1)[1].split("}, [fetchCourses, visibleViewMode]);", 1)[0]
        self.assertIn('if (visibleViewMode !== "list") return;', fetch_effect)
        self.assertIn("void fetchCourses();", fetch_effect)
        self.assertNotIn("if (routeRequestsMap) return;", fetch_effect)
        self.assertIn("[fetchCourses, visibleViewMode]", learn_page)
        self.assertIn('onClick={() => switchView("list")}', learn_page)

    def test_tab_history_reads_current_browser_query_after_push_state(self) -> None:
        page = (PROJECT_ROOT / "frontend/src/app/[locale]/learn/page.tsx").read_text(encoding="utf-8")
        helper = (PROJECT_ROOT / "frontend/src/lib/learn-route-state.mjs").read_text(encoding="utf-8")
        self.assertIn("const routeSearch = searchParams.toString()", page)
        self.assertIn("parseMapFocus(routeSearch)", page)
        self.assertIn("getLearnHydrationSearch(", page)
        self.assertIn('setMapFocus(parseMapFocus(window.location.search))', page)
        self.assertIn('routeSearch !== ""', helper)
        self.assertIn('writeUrlQuery({ view: nextView === "map" ? "map" : null }, "push")', page)
        writer = (PROJECT_ROOT / "frontend/src/lib/url-query.ts").read_text(encoding="utf-8")
        self.assertIn('window.history[mode === "push" ? "pushState" : "replaceState"]', writer)
        self.assertIn('if (next === current) return;', writer)
        self.assertIn('`${url.pathname}${url.search}${url.hash}`', writer)
        self.assertNotIn('routeSearch || (typeof window', page)

    def test_map_defaults_to_direct_learning_content_and_node_focus_expansion(self) -> None:
        component = (PROJECT_ROOT / "frontend/src/components/learn/KnowledgeGraphMap.tsx").read_text(encoding="utf-8")
        helper = (PROJECT_ROOT / "frontend/src/lib/map-ui-state.mjs").read_text(encoding="utf-8")
        route_helper = (PROJECT_ROOT / "frontend/src/lib/learn-route-state.mjs").read_text(encoding="utf-8")
        panel = (PROJECT_ROOT / "frontend/src/components/wargame/LearningPathPanel.tsx").read_text(encoding="utf-8")
        source = (PROJECT_ROOT / "services/learn/app/api/knowledge_graph.py").read_text(encoding="utf-8")
        content_defaults = helper.split("export const MAP_CONTENT_TYPES =", 1)[1].split(";", 1)[0]
        for kind in ("challenge", "course", "lesson", "knowledge_tag"):
            self.assertIn(f'"{kind}"', content_defaults)
            self.assertIn(f'"{kind}"', route_helper)
        for kind in ("hint", "event", "assignment"):
            self.assertNotIn(f'"{kind}"', content_defaults)
            self.assertNotIn(f'"{kind}"', route_helper)
        self.assertIn('value !== "tag_parent"', helper)
        self.assertIn('focus?.type === "knowledge_tag"', component)
        self.assertIn('relations.add("tag_parent")', component)
        self.assertIn("const types = new Set(tools.types)", component)
        self.assertIn("if (!isMapContentType(focusType)) return null;", panel)
        for marker in ("_V2_DEFAULT_CONTENT_TYPES = {", "_V2_DEFAULT_RELATION_TYPES = {", "default=_V2_DEFAULT_CONTENT_TYPES", "default=_V2_DEFAULT_RELATION_TYPES", "public_tag_mapping_clause("):
            self.assertIn(marker, source)

    def test_learn_map_mode_does_not_render_course_list_filters(self) -> None:
        page = (PROJECT_ROOT / "frontend" / "src" / "app" / "[locale]" / "learn" / "page.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn('visibleViewMode === "list"', page)
        self.assertIn('search-input-wrapper w-full sm:max-w-xs', page)
        self.assertIn('import { CategoryFilter } from "@/components/wargame/CategoryFilter";', page)
        self.assertIn("const LEARN_DIFFICULTIES = [", page)
        self.assertIn('visibleViewMode === "map"', page)
        self.assertIn('t("viewList")', page)
        self.assertIn('t("viewMap")', page)
        self.assertIn('onClick={() => switchView("list")}', page)
        self.assertIn('onClick={() => switchView("map")}', page)

    def test_rule_mapping_reconcile_is_explicit_and_preserves_manual_rows(self) -> None:
        tags_api = (PROJECT_ROOT / "services" / "learn" / "app" / "api" / "knowledge_tags.py").read_text(
            encoding="utf-8"
        )
        schema = (PROJECT_ROOT / "services" / "learn" / "app" / "schemas" / "knowledge.py").read_text(
            encoding="utf-8"
        )
        for marker in (
            '@router.post("/reconcile"',
            "Challenge.tags",
            '"origin": "rule"',
            "on_conflict_do_nothing",
            "수동/import 매핑은 절대",
            "_add_mapping_audit",
        ):
            self.assertIn(marker, tags_api)
        self.assertIn('origin: Literal["manual", "import", "rule", "model_candidate"]', schema)

    def test_model_candidate_tag_rows_are_not_public_graph_edges(self) -> None:
        relations = (PROJECT_ROOT / "services" / "learn" / "app" / "utils" / "knowledge_relations.py").read_text(
            encoding="utf-8"
        )
        graph = (PROJECT_ROOT / "services" / "learn" / "app" / "api" / "knowledge_graph.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def public_tag_mapping_clause(mapping_model):", relations)
        self.assertIn('mapping_model.origin != "model_candidate"', relations)
        self.assertIn("mapping_model.approved_at.is_not(None)", relations)
        self.assertGreaterEqual(graph.count("public_tag_mapping_clause("), 10)

    def test_focus_combines_tag_affinity_and_parent_without_reusing_recursive_cte(self) -> None:
        """Depth-two tag expansion must not turn the focus endpoint into a 500."""
        source = (PROJECT_ROOT / "services" / "learn" / "app" / "api" / "knowledge_graph.py").read_text(
            encoding="utf-8"
        )
        body = source.split("async def _v2_tag_hierarchy_projection_edges(", 1)[1].split(
            "async def _v2_course_structure_projection_edges(", 1
        )[0]
        self.assertIn("public_tag_result = await db.execute(public_knowledge_tag_ids_query())", body)
        self.assertIn("public_tag_ids = {str(tag_id)", body)
        self.assertIn("KnowledgeTag.id.in_([int(tag_id)", body)
        self.assertIn("KnowledgeTag.parent_id.in_([int(tag_id)", body)
        self.assertNotIn('public_knowledge_tag_ids_query().subquery("public_graph_tag_ids")', body)
        self.assertNotIn("select(public_tag_ids.c.tag_id)", body)
        # resolve_content returns ResolvedKnowledgeContent; the hierarchy
        # projection must use its canonical entity_id instead of ORM `.id`.
        self.assertIn("parent_tag_id=parent.entity_id", body)
        self.assertIn("next_frontier.add(parent.entity_id)", body)
        self.assertNotIn("parent.id", body)

    def test_tag_parent_projection_normalizes_legacy_naive_timestamps(self) -> None:
        source = (PROJECT_ROOT / "services" / "learn" / "app" / "api" / "knowledge_graph.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def _graph_utc_datetime(value: datetime | None) -> datetime:", source)
        self.assertIn("_graph_utc_datetime(child_content.source_updated_at)", source)
        self.assertIn("_graph_utc_datetime(parent.source_updated_at)", source)

    def test_smoke_shell_continuations_are_single_backslashes(self) -> None:
        common = (PROJECT_ROOT / "infra" / "scripts" / "lib" / "ctfctl-common.sh").read_text(encoding="utf-8")
        for line_number, line in enumerate(common.splitlines(), start=1):
            self.assertFalse(
                line.rstrip().endswith("\\\\"),
                f"line {line_number} has an escaped shell continuation",
            )

    def test_search_probe_uses_entity_id_independent_of_json_key_order(self) -> None:
        source = (PROJECT_ROOT / "infra/scripts/lib/ctfctl-common.sh").read_text(encoding="utf-8")
        program = source.split("| python3 -c '\n", 1)[1].split("\n')\"", 1)[0]
        for payload, expected in (
            ({"items": [{"id": "course:abc-123", "name": "Course", "entity_id": "abc-123", "type": "course"}]}, "course\tabc-123\n"),
            ({"items": []}, ""),
        ):
            result = subprocess.run([sys.executable, "-B", "-c", program], input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, expected)
        for item in (
            {"type": "unknown", "entity_id": "1"},
            {"type": "challenge", "entity_id": "1&depth=3"},
            {"type": "challenge", "id": "challenge:1"},
        ):
            result = subprocess.run([sys.executable, "-B", "-c", program], input=json.dumps({"items": [item]}), capture_output=True, text=True, encoding="utf-8", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
