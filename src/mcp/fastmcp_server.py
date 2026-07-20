"""FastMCP-based OpenFOAM Agent Server.

This module provides a modern MCP server implementation using FastMCP,
exposing OpenFOAM simulation capabilities through clean, well-typed interfaces.
"""

import asyncio
import os
import json
from typing import Dict, List, Optional, Any

from fastmcp import FastMCP, Context
from pydantic import BaseModel, Field

# Import existing services
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.plan import (
    resolve_case_dir,
    retrieve_references,
    generate_simulation_plan
)
from services.input_writer import initial_write
from services.run_local import run_allrun_and_collect_errors

from utils import FoamPydantic
from services.review import review_error_logs
from services.visualization import (
    ensure_foam_file,
    generate_pyvista_script,
    run_pyvista_script,
    fix_pyvista_script
)
from config import Config


# Global configuration
global_config = Config()


# Create FastMCP server
mcp = FastMCP(
    name="Foam-Agent",
    version="2.0.0",
    instructions="""
Foam-Agent is a multi-agent framework that automates the entire OpenFOAM-based CFD simulation workflow from a single natural language prompt.
By managing the full pipeline—from meshing and case setup to execution and post-processing—Foam-Agent dramatically lowers the expertise barrier for Computational Fluid Dynamics.

IMPORTANT: This tool targets **Foundation OpenFOAM v10** (openfoam.org) exclusively. All generated case files,
dictionary names, and solver commands follow Foundation v10 conventions. It is NOT compatible with ESI OpenFOAM
(openfoam.com, e.g., v2312, v2406, v2512), which uses different file names and solver binaries. For example:
- Foundation v10 uses `constant/momentumTransport`, ESI uses `constant/turbulenceProperties`
- Foundation v10 uses `constant/physicalProperties`, ESI uses `constant/thermophysicalProperties`
- Foundation v10 uses `buoyantFoam`, ESI uses `buoyantBoussinesqPimpleFoam`

To run simulations, you must have Foundation OpenFOAM v10 installed, or use the Foam-Agent Docker image
which includes it pre-installed.
"""
)


# ============================================================================
# Tool: plan
# ============================================================================

class PlanRequest(BaseModel):
    """Request to plan simulation structure."""
    user_requirement: str = Field(description="User requirements for the simulation")


class PlanResponse(BaseModel):
    """Response from simulation planning."""
    subtasks: List[Dict[str, str]] = Field(description="List of subtasks with file and folder information")
    case_name: str = Field(description="Generated case name")
    case_solver: str = Field(description="OpenFOAM solver to use")
    case_domain: str = Field(description="Simulation domain (e.g., 'fluid', 'solid')")
    case_category: str = Field(description="Case category (e.g., 'tutorial', 'advanced')")


@mcp.tool(name="plan")
async def plan(
    request: PlanRequest,
    ctx: Context
) -> PlanResponse:
    """Plan the simulation structure by analyzing requirements and generating subtasks.

    This function uses AI to break down user requirements into manageable subtasks
    for OpenFOAM file generation. The plan targets Foundation OpenFOAM v10 (openfoam.org)
    conventions — solvers, dictionary names, and file structure all follow v10 defaults.
    Not compatible with ESI OpenFOAM (openfoam.com).
    """
    try:
        await ctx.info("Planning simulation structure from user requirements")
        
        # Load case statistics, available domains, categories, and solvers
        case_stats_path = os.path.join(global_config.database_path, "raw", "openfoam_case_stats.json")
        with open(case_stats_path, 'r') as f:
            case_stats = json.load(f)
        
        # Generate simulation plan
        plan_data = generate_simulation_plan(
            user_requirement=request.user_requirement,
            case_stats=case_stats,
            case_dir="",  # Will be resolved later
            searchdocs=global_config.searchdocs,
        )
        
        await ctx.info(f"Generated {len(plan_data['subtasks'])} subtasks")
        
        # Convert subtasks to PlanResponse format
        subtasks = [{"file": s["file_name"], "folder": s["folder_name"]} for s in plan_data["subtasks"]]
        
        return PlanResponse(
            subtasks=subtasks,
            case_name=plan_data["case_name"],
            case_solver=plan_data["case_solver"],
            case_domain=plan_data["case_domain"],
            case_category=plan_data["case_category"]
        )
        
    except Exception as e:
        await ctx.error(f"Failed to plan simulation: {str(e)}")
        raise


# ============================================================================
# Tool: input_writer
# ============================================================================

class GenerateFilesRequest(BaseModel):
    """Request to generate OpenFOAM files."""
    case_name: str = Field(description="Case name (from plan response)")
    subtasks: List[Dict[str, str]] = Field(description="List of subtasks to generate files for")
    user_requirement: str = Field(description="User requirements")
    case_solver: str = Field(description="OpenFOAM solver to use")
    case_domain: str = Field(description="Simulation domain")
    case_category: str = Field(description="Case category")


class GenerateFilesResponse(BaseModel):
    """Response from file generation."""
    case_dir: str = Field(description="Path to the case directory")
    foamfiles: FoamPydantic = Field(description="Generated OpenFOAM files with metadata")
    allrun_script: str = Field(description="Path to the generated Allrun script")


@mcp.tool(name="input_writer")
async def input_writer(
    request: GenerateFilesRequest,
    ctx: Context
) -> GenerateFilesResponse:
    """Generate OpenFOAM input files based on subtasks and requirements.

    This function creates all necessary OpenFOAM input files (system/, constant/, 0/)
    using Foundation OpenFOAM v10 (openfoam.org) conventions. Generated files use v10
    dictionary names (e.g., momentumTransport, physicalProperties) and solver binaries
    (e.g., buoyantFoam). These files are NOT compatible with ESI OpenFOAM (openfoam.com)
    without manual adaptation.
    """
    try:
        await ctx.info(f"Generating OpenFOAM files for case: {request.case_name}")

        # Resolve case directory
        case_dir = resolve_case_dir(
            case_name=request.case_name,
            case_dir="",
            run_times=global_config.run_times
        )

        await ctx.info(f"Case directory: {case_dir}")

        # Load case statistics and retrieve references
        case_stats_path = os.path.join(global_config.database_path, "raw", "openfoam_case_stats.json")
        with open(case_stats_path, 'r') as f:
            case_stats = json.load(f)

        # Build case info from request
        case_info = {
            "case_name": request.case_name,
            "case_solver": request.case_solver,
            "case_domain": request.case_domain,
            "case_category": request.case_category
        }

        await ctx.info(f"Case info: {case_info}")

        # Retrieve references
        tutorial_reference, dir_structure, dir_counts_str, allrun_reference, _ = retrieve_references(
            case_name=case_info["case_name"],
            case_solver=case_info["case_solver"],
            case_domain=case_info["case_domain"],
            case_category=case_info["case_category"],
            searchdocs=global_config.searchdocs,
        )

        # Convert subtasks format from {file, folder} to {file_name, folder_name}
        converted_subtasks = []
        for st in request.subtasks:
            if isinstance(st, dict):
                # Handle both formats: {file, folder} or {file_name, folder_name}
                file_name = st.get("file_name") or st.get("file")
                folder_name = st.get("folder_name") or st.get("folder")
                if file_name and folder_name:
                    converted_subtasks.append({
                        "file_name": file_name,
                        "folder_name": folder_name
                    })
                else:
                    raise ValueError(f"Invalid subtask format: {st}. Must have 'file'/'file_name' and 'folder'/'folder_name'")
            else:
                raise ValueError(f"Invalid subtask type: {type(st)}. Expected dict, got {st}")

        await ctx.info(f"converted_subtasks: {converted_subtasks}")

        # Create a sync callback that bridges to async ctx.report_progress.
        # initial_write() is synchronous and will run in a worker thread via
        # asyncio.to_thread(), so we use run_coroutine_threadsafe to schedule
        # progress notifications on the event loop from that thread.
        loop = asyncio.get_running_loop()

        def progress_callback(current: int, total: int, message: str) -> None:
            future = asyncio.run_coroutine_threadsafe(
                ctx.report_progress(current, total, message),
                loop
            )
            try:
                future.result(timeout=5.0)
            except Exception:
                pass  # Don't fail generation if progress reporting fails

        # Run blocking initial_write in a thread to keep the event loop free
        # for sending progress notifications
        result = await asyncio.to_thread(
            initial_write,
            case_dir=case_dir,
            subtasks=converted_subtasks,
            user_requirement=request.user_requirement,
            tutorial_reference=tutorial_reference,
            case_solver=request.case_solver,
            case_info=str(case_info),
            allrun_reference=allrun_reference,
            database_path=str(global_config.database_path),
            searchdocs=global_config.searchdocs,
            progress_callback=progress_callback,
        )

        await ctx.info(f"result: {result}")

        # Get foamfiles from result
        foamfiles = result.get("foamfiles")
        if not foamfiles:
            raise ValueError("No foamfiles returned from initial_write")

        allrun_script = os.path.join(case_dir, "Allrun")

        num_files = len(foamfiles.list_foamfile) if hasattr(foamfiles, "list_foamfile") else 0
        await ctx.info(f"Generated {num_files} OpenFOAM files in {case_dir}")

        return GenerateFilesResponse(
            case_dir=case_dir,
            foamfiles=foamfiles,
            allrun_script=allrun_script
        )

    except Exception as e:
        await ctx.error(f"Failed to generate OpenFOAM files: {str(e)}")
        raise


# ============================================================================
# Tool: run
# ============================================================================

class RunSimulationRequest(BaseModel):
    """Request to run local simulation."""
    case_dir: str = Field(description="Path to the case directory")
    timeout: int = Field(default=3600, description="Timeout in seconds")


class RunSimulationResponse(BaseModel):
    """Response from simulation run."""
    status: str = Field(description="Run status: 'success' or 'failed'")
    errors: List[str] = Field(description="List of errors found")
    log_files: Dict[str, str] = Field(description="Paths to log files")


@mcp.tool(name="run")
async def run(
    request: RunSimulationRequest,
    ctx: Context
) -> RunSimulationResponse:
    """Run the OpenFOAM simulation locally.

    This function executes the Allrun script and collects any errors.
    Requires Foundation OpenFOAM v10 (openfoam.org) installed and sourced in PATH.
    Will not work with ESI OpenFOAM (openfoam.com).
    """
    try:
        await ctx.info(f"Running simulation in directory: {request.case_dir}")
        
        # Validate case directory exists
        if not os.path.exists(request.case_dir):
            raise ValueError(f"Case directory does not exist: {request.case_dir}")
        
        # Run locally
        error_logs = run_allrun_and_collect_errors(
            case_dir=request.case_dir,
            timeout=request.timeout,
            max_retries=3
        )
        
        # Convert error logs to strings if they're dictionaries
        errors = []
        for err in error_logs:
            if isinstance(err, dict):
                # Format: "file: error_content"
                file_name = err.get("file", "unknown")
                error_content = err.get("error_content", str(err))
                errors.append(f"{file_name}: {error_content}")
            else:
                errors.append(str(err))
        
        # Prepare log file paths (not content)
        log_files = {}
        out_path = os.path.join(request.case_dir, 'Allrun.out')
        err_path = os.path.join(request.case_dir, 'Allrun.err')
        
        if os.path.exists(out_path):
            log_files['Allrun.out'] = out_path
        
        if os.path.exists(err_path):
            log_files['Allrun.err'] = err_path
        
        status = "success" if not errors else "failed"
        
        await ctx.info(f"Simulation {status} with {len(errors)} error(s)")
        
        return RunSimulationResponse(
            status=status,
            errors=errors,
            log_files=log_files
        )
            
    except Exception as e:
        await ctx.error(f"Failed to run simulation: {str(e)}")
        raise


# ============================================================================
# Tool: review
# ============================================================================

class ReviewRequest(BaseModel):
    """Request to review simulation errors."""
    case_dir: str = Field(description="Path to the case directory")
    errors: List[str] = Field(description="List of error messages from simulation")
    user_requirement: str = Field(description="Original user requirements")


class ReviewResponse(BaseModel):
    """Response from simulation review."""
    analysis: str = Field(description="Analysis of simulation errors")


@mcp.tool(name="review")
async def review(
    request: ReviewRequest,
    ctx: Context
) -> ReviewResponse:
    """Review simulation errors and suggest improvements.

    This function analyzes simulation errors and provides suggestions for fixes.
    All analysis is based on Foundation OpenFOAM v10 (openfoam.org) conventions and
    tutorial references. Not applicable to ESI OpenFOAM (openfoam.com) cases.
    """
    try:
        await ctx.info(f"Reviewing errors for case directory: {request.case_dir}")
        
        # Validate case directory exists
        if not os.path.exists(request.case_dir):
            raise ValueError(f"Case directory does not exist: {request.case_dir}")
        
        # Load case statistics
        case_stats_path = os.path.join(global_config.database_path, "raw", "openfoam_case_stats.json")
        with open(case_stats_path, 'r') as f:
            case_stats = json.load(f)
        
        # Extract case name from case_dir for reference lookup
        case_name = os.path.basename(request.case_dir)
        
        # Get tutorial reference
        case_info = {
            "case_name": case_name,
            "case_solver": "simpleFoam",  # Default
            "case_domain": "fluid",
            "case_category": "tutorial"
        }
        
        tutorial_reference, _, _, _, _ = retrieve_references(
            case_name=case_info["case_name"],
            case_solver=case_info["case_solver"],
            case_domain=case_info["case_domain"],
            case_category=case_info["case_category"],
            searchdocs=global_config.searchdocs,
        )
        
        # Read current foamfiles from case directory for review context
        await ctx.info("Reading OpenFOAM files for review context...")
        from utils import read_case_foamfiles
        foamfiles = read_case_foamfiles(request.case_dir)
        await ctx.info(f"Read {len(foamfiles.list_foamfile)} file(s) for review")
        
        # Review results - directly call review_error_logs
        review_content, _ = review_error_logs(
            tutorial_reference=tutorial_reference,
            foamfiles=foamfiles,
            error_logs=request.errors,
            user_requirement=request.user_requirement,
            history_text=None
        )
        
        await ctx.info(f"Review completed, found {len(request.errors)} error(s)")
        
        # Format response (suggestions and issues are empty as review_error_logs only returns analysis)
        return ReviewResponse(
            analysis=review_content
        )
        
    except Exception as e:
        await ctx.error(f"Failed to review results: {str(e)}")
        raise


# ============================================================================
# Tool: apply_fixes
# ============================================================================

class ApplyFixesRequest(BaseModel):
    """Request to apply fixes to an OpenFOAM case based on review analysis."""
    case_dir: str = Field(description="Path to the OpenFOAM case directory")
    error_logs: List[str] = Field(description="List of error log messages from simulation")
    review_analysis: str = Field(description="Review analysis with fix suggestions from the review tool. Must be provided.")
    user_requirement: str = Field(description="Original user requirements or simulation description for context")


class ApplyFixesResponse(BaseModel):
    """Response from applying fixes."""
    updated_files: List[str] = Field(description="List of file paths that were updated")
    status: str = Field(description="Fix application status ('ok' or 'no_changes')")


@mcp.tool(name="apply_fixes")
async def apply_fixes(
    request: ApplyFixesRequest,
    ctx: Context
) -> ApplyFixesResponse:
    """Apply fixes to the OpenFOAM case files based on review analysis.

    This tool rewrites OpenFOAM files to fix errors identified during review.
    It must be called after the 'review' tool has provided analysis.
    All fixes target Foundation OpenFOAM v10 (openfoam.org) conventions.
    
    The tool directly calls rewrite_files which handles:
    - Reading current foamfiles and directory structure from case_dir
    - Using LLM to generate corrected file contents based on review_analysis
    - Writing updated files back to the case directory
    
    Workflow:
    1. First call 'review' tool to get review_analysis
    2. Then call 'apply_fixes' with the review_analysis to rewrite files
    
    Args:
        request: ApplyFixesRequest containing:
            - case_dir: Path to the OpenFOAM case directory
            - error_logs: List of error messages from simulation
            - review_analysis: Analysis and fix suggestions from review tool (required)
            - user_requirement: Original user requirements (optional)
    
    Returns:
        ApplyFixesResponse with list of updated files and status
    
    Raises:
        ValueError: If case directory does not exist or review_analysis is empty
        RuntimeError: If fix application fails
    
    Example:
        # Two-step workflow (review first, then fix)
        review_resp = await review(case_dir, errors, user_requirement)
        fix_resp = await apply_fixes(case_dir, errors, review_resp.analysis, user_requirement)
    """
    try:
        await ctx.info(f"Applying fixes for case directory: {request.case_dir}")
        
        # Validate case directory exists
        if not os.path.exists(request.case_dir):
            raise ValueError(f"Case directory does not exist: {request.case_dir}")
        
        # Validate review_analysis is provided
        if not request.review_analysis or request.review_analysis.strip() == "":
            raise ValueError(
                "review_analysis is required. Please call the 'review' tool first "
                "to get review analysis, then provide it to this tool."
            )
        
        await ctx.info("Rewriting OpenFOAM files based on review analysis...")
        
        # Directly call rewrite_files - it now handles file reading internally
        from services.input_writer import rewrite_files
        
        result = rewrite_files(
            case_dir=request.case_dir,
            error_logs=request.error_logs,
            review_analysis=request.review_analysis,
            rewrite_plan=None,
            user_requirement=request.user_requirement
            # foamfiles and dir_structure will be read automatically if None
        )
        
        # Extract written file paths
        written_files = []
        if result.get("foamfiles") and hasattr(result["foamfiles"], "list_foamfile"):
            for foamfile in result["foamfiles"].list_foamfile:
                file_path = os.path.join(request.case_dir, foamfile.folder_name, foamfile.file_name)
                written_files.append(file_path)
        
        status = "ok" if written_files else "no_changes"
        
        await ctx.info(f"Successfully applied fixes. Updated {len(written_files)} file(s)")
        
        return ApplyFixesResponse(
            updated_files=written_files,
            status=status
        )
        
    except Exception as e:
        await ctx.error(f"Failed to apply fixes: {str(e)}")
        raise


# ============================================================================
# Tool: visualization
# ============================================================================

class VisualizationRequest(BaseModel):
    """Request to generate visualization."""
    case_dir: str = Field(description="Path to the case directory")
    quantity: str = Field(description="Quantity to visualize (e.g., 'velocity', 'pressure')")
    visualization_type: str = Field(default="pyvista", description="Visualization type")


class VisualizationResponse(BaseModel):
    """Response from visualization generation."""
    artifacts: List[str] = Field(description="List of generated visualization files")
    script: str = Field(description="Visualization script")


@mcp.tool(name="visualization")
async def visualization(
    request: VisualizationRequest,
    ctx: Context
) -> VisualizationResponse:
    """Generate visualization for the simulation results.

    This function creates visualization artifacts using PyVista.
    Expects case output from a Foundation OpenFOAM v10 (openfoam.org) simulation.
    """
    try:
        await ctx.info(f"Generating visualization for case directory: {request.case_dir}")
        
        # Validate case directory exists
        if not os.path.exists(request.case_dir):
            raise ValueError(f"Case directory does not exist: {request.case_dir}")
        
        # Ensure foam file exists
        foam_file = ensure_foam_file(request.case_dir)
        
        # Generate visualization script
        script = generate_pyvista_script(
            case_dir=request.case_dir,
            foam_file=foam_file,
            user_requirement=request.quantity,
            previous_errors=[]
        )
        
        # Run visualization script
        ok, img, errs = run_pyvista_script(request.case_dir, script)
        
        if ok and img:
            artifacts = [img]
        else:
            # Try to fix the script
            fixed = fix_pyvista_script(foam_file, script, errs)
            ok2, img2, errs2 = run_pyvista_script(request.case_dir, fixed)
            artifacts = [img2] if ok2 and img2 else []
        
        await ctx.info(f"Generated {len(artifacts)} visualization artifact(s)")
        
        return VisualizationResponse(
            artifacts=artifacts,
            script=script
        )
        
    except Exception as e:
        await ctx.error(f"Failed to generate visualization: {str(e)}")
        raise


# ============================================================================
# Astra design-session tools: plan_design, build_geometry
# ============================================================================

from services import global_llm_service
from services.intent import run_intent
from services.geometry import GeometryRejected, apply_overrides, build_geometry as build_geometry_svc
from services import design_session as ds

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from domain_packs.bwb.schema import BWBParams, MissionDerived
from domain_packs.bwb.atmosphere import atmosphere_provenance


class PlanDesignRequest(BaseModel):
    """Turn a casual mission request into a reviewable design plan."""
    prompt: str = Field(description="The user's mission request in plain language, e.g. "
                                    "'drone that carries 2 kg at 25 m/s around 100 m altitude'")
    session_id: Optional[str] = Field(default=None,
                                      description="Existing design session to revise; omit to start a new one")
    revision_note: Optional[str] = Field(default=None,
                                         description="User feedback from reviewing a previous plan version")


class PlanDesignResponse(BaseModel):
    session_id: str
    version: int
    mission: Dict[str, Any] = Field(description="Parsed mission spec")
    derived: Dict[str, Any] = Field(description="Derived physics: MTOW, S_ref, required CL, Re, targets")
    geometry_params: Dict[str, Any] = Field(description="Proposed BWB parameters (editable via build_geometry overrides)")
    assumptions: List[str] = Field(description="Every default/assumption made — review these")
    plan_dir: str


@mcp.tool(name="plan_design")
async def plan_design(request: PlanDesignRequest, ctx: Context) -> PlanDesignResponse:
    """Stage 1 of the BWB design loop: parse a casual mission request into structured
    mission numbers, derived aerodynamic targets (required CL, Reynolds number, wing area)
    and a proposed parametric BWB geometry. Nothing is built yet — review the assumptions
    and geometry parameters, then call build_geometry (optionally with overrides), or call
    plan_design again with a revision_note to change the plan."""
    try:
        sid = request.session_id or ds.create_session(label=request.prompt[:60])
        await ctx.info(f"Design session {sid}: deriving mission plan")

        result = run_intent(request.prompt, global_llm_service, request.revision_note)
        d = result.mission_derived

        vdir = ds.new_version_dir(sid, "plan")
        with open(os.path.join(vdir, "mission_derived.json"), "w") as f:
            f.write(d.model_dump_json(indent=2))
        with open(os.path.join(vdir, "enriched_requirement.txt"), "w") as f:
            f.write(result.enriched_requirement)
        version = ds.record_version(sid, "plan", {
            "prompt": request.prompt,
            "revision_note": request.revision_note,
            **atmosphere_provenance(d.atmosphere),
        })

        return PlanDesignResponse(
            session_id=sid,
            version=version,
            mission=d.mission.model_dump(),
            derived={
                "mtow_kg": round(d.mtow_kg, 2),
                "air_density_kgm3": round(d.air_density_kgm3, 4),
                "air_temperature_k": round(d.atmosphere.temperature_k, 3),
                "air_pressure_pa": round(d.atmosphere.pressure_pa, 2),
                "dynamic_viscosity_kgms": d.atmosphere.dynamic_viscosity_kgms,
                "kinematic_viscosity_m2s": d.atmosphere.kinematic_viscosity_m2s,
                "atmosphere": d.atmosphere.model_dump(),
                "s_ref_target_m2": round(d.s_ref_target_m2, 4),
                "wing_loading_kgm2": round(d.wing_loading_kgm2, 2),
                "required_cl": round(d.required_cl, 3),
                "reynolds_mac": f"{d.reynolds_mac:.3g}",
                "turbulence_model": d.turbulence_model,
                "static_margin_target": d.static_margin_target,
            },
            geometry_params=d.params.model_dump(),
            assumptions=d.assumptions,
            plan_dir=vdir,
        )
    except Exception as e:
        await ctx.error(f"plan_design failed: {e}")
        raise


class BuildGeometryRequest(BaseModel):
    """Build (or rebuild) the parametric BWB geometry for a session."""
    session_id: str
    overrides: Optional[Dict[str, float]] = Field(
        default=None,
        description="Geometry parameter deltas from user review, e.g. {'span_m': 1.8, "
                    "'sweep_outer_deg': 28}. Applied on top of the latest geometry version "
                    "(or the plan's proposal if no geometry exists yet).")


class BuildGeometryResponse(BaseModel):
    session_id: str
    version: int
    passed: bool
    gates: List[Dict[str, Any]]
    summary: Dict[str, Any] = Field(description="S_ref, AR, MAC, span, facets, watertight")
    params: Dict[str, Any]
    renders: List[str] = Field(description="PNG paths (iso/top/front/side) — display these to the user")
    artifacts: Dict[str, str] = Field(description="stl / vsp3 / degen_csv / report paths")
    geometry_dir: str


@mcp.tool(name="build_geometry")
async def build_geometry(request: BuildGeometryRequest, ctx: Context) -> BuildGeometryResponse:
    """Stage 2 of the BWB design loop: build the OpenVSP geometry from the session's
    planned parameters (plus any user overrides), run geometry validation gates
    (parameter ranges, watertight STL, bounding-box sanity), export STL/VSP3/degen-geom,
    and render review images. Show the renders to the user for approval before meshing.
    A new call creates a new geometry version and invalidates downstream stages."""
    try:
        # Parameter source: latest geometry version if present, else the plan
        geo_dir = ds.latest_dir(request.session_id, "geometry")
        if geo_dir and os.path.exists(os.path.join(geo_dir, "params.json")):
            with open(os.path.join(geo_dir, "params.json")) as f:
                params = BWBParams.model_validate_json(f.read())
        else:
            plan_dir = ds.latest_dir(request.session_id, "plan")
            if not plan_dir:
                raise ValueError("no plan in this session — call plan_design first")
            with open(os.path.join(plan_dir, "mission_derived.json")) as f:
                params = MissionDerived.model_validate_json(f.read()).params

        if request.overrides:
            await ctx.info(f"Applying geometry overrides: {request.overrides}")
            params = apply_overrides(params, request.overrides)

        vdir = ds.new_version_dir(request.session_id, "geometry")
        await ctx.info(f"Building OpenVSP geometry in {vdir}")
        report = build_geometry_svc(params, vdir)

        version = ds.record_version(request.session_id, "geometry", {
            "overrides": request.overrides or {},
            "passed": report.all_gates_pass,
        })
        renders = [v for k, v in report.artifacts.items() if k.startswith("render_")]
        return BuildGeometryResponse(
            session_id=request.session_id,
            version=version,
            passed=report.all_gates_pass,
            gates=[g.model_dump() for g in report.gates],
            summary={
                "s_ref_m2": round(report.s_ref_m2, 4),
                "aspect_ratio": round(report.aspect_ratio, 2),
                "mac_m": round(report.mean_aero_chord_m, 4),
                "span_m": report.params.span_m,
                "n_facets": report.n_facets,
                "watertight": report.watertight,
            },
            params=report.params.model_dump(),
            renders=renders,
            artifacts={k: v for k, v in report.artifacts.items() if not k.startswith("render_")},
            geometry_dir=vdir,
        )
    except GeometryRejected as e:
        await ctx.error(f"geometry rejected: {e}")
        raise
    except Exception as e:
        await ctx.error(f"build_geometry failed: {e}")
        raise


class SetRequirementsRequest(BaseModel):
    """Solve the geometry from engineering requirements (requirements-first)."""
    session_id: str
    requirements: Dict[str, Any] = Field(
        description="Partial DesignRequirements fields, merged over the session's saved "
                    "requirements: mtow_kg, cruise_speed_ms, cruise_altitude_m, launch, "
                    "cl_cruise_target, ld_min, v_stall_max_ms, span_max_m. "
                    "Set a target to null to drop that requirement.")
    keep_shape: bool = Field(default=True,
                             description="Hold the current design's shape fractions so the "
                                         "change morphs the existing design instead of resetting it")
    apply: bool = Field(default=False,
                        description="True: also OpenVSP-build the solved geometry as a new "
                                    "geometry version (slow); False: solve only")


class SetRequirementsResponse(BaseModel):
    session_id: str
    requirements_version: int
    proposed_params: Dict[str, Any]
    panel: Dict[str, Any] = Field(description="Closed-form metric panel of the solved design")
    statuses: List[Dict[str, Any]] = Field(description="Per-requirement target vs achieved")
    binding_constraint: Optional[str] = Field(description="Machine key of the limit that "
                                              "stopped the solver, or null when all targets met")
    binding_explanation: str
    notes: List[str]
    geometry_version: Optional[int] = Field(description="Set when apply=True built the STL")


@mcp.tool(name="set_requirements")
async def set_requirements(request: SetRequirementsRequest, ctx: Context) -> SetRequirementsResponse:
    """Requirements-first design: state engineering requirements (cruise CL target,
    minimum L/D, maximum stall speed, span cap, flight condition) and get back a
    deterministically solved BWB geometry with per-requirement status and the named
    binding constraint when a target is unreachable. Closed-form inverse sizing —
    microseconds, no LLM, no solver. Shares the requirements store with the cockpit,
    so both stay in sync. Use apply=true to also rebuild the authoritative STL."""
    from domain_packs.bwb.inverse import solve_requirements
    from domain_packs.bwb.requirements import DesignRequirements
    try:
        req_fields = set(DesignRequirements.model_fields)
        saved = ds.latest_requirements(request.session_id) or {}
        merged = {k: v for k, v in saved.items() if k in req_fields}
        if not merged:  # session predates the requirements store: seed from the plan
            plan_dir = ds.latest_dir(request.session_id, "plan")
            if plan_dir:
                with open(os.path.join(plan_dir, "mission_derived.json")) as f:
                    d = MissionDerived.model_validate_json(f.read())
                merged = {"mtow_kg": d.mtow_kg,
                          "cruise_speed_ms": d.mission.cruise_speed_ms,
                          "cruise_altitude_m": d.mission.cruise_altitude_m,
                          "launch": d.mission.launch}
        merged.update({k: v for k, v in request.requirements.items() if k in req_fields})
        requirements = DesignRequirements.model_validate(merged)

        shape_from = None
        if request.keep_shape:
            geo_dir = ds.latest_dir(request.session_id, "geometry")
            if geo_dir and os.path.exists(os.path.join(geo_dir, "params.json")):
                with open(os.path.join(geo_dir, "params.json")) as f:
                    shape_from = BWBParams.model_validate_json(f.read())

        result = solve_requirements(requirements, shape_from=shape_from)
        version = ds.save_requirements(request.session_id, requirements.model_dump())
        await ctx.info(f"Requirements v{version} solved: "
                       + (result.binding_explanation if result.binding_constraint
                          else "all targets met"))

        geometry_version = None
        if request.apply:
            vdir = ds.new_version_dir(request.session_id, "geometry")
            report = build_geometry_svc(result.params, vdir)
            geometry_version = ds.record_version(request.session_id, "geometry", {
                "source": "set_requirements",
                "requirements_version": version,
                "passed": report.all_gates_pass,
            })

        return SetRequirementsResponse(
            session_id=request.session_id,
            requirements_version=version,
            proposed_params=result.params.model_dump(),
            panel=json.loads(result.panel.model_dump_json()),
            statuses=[s.model_dump() for s in result.requirements],
            binding_constraint=result.binding_constraint,
            binding_explanation=result.binding_explanation,
            notes=result.notes,
            geometry_version=geometry_version,
        )
    except Exception as e:
        await ctx.error(f"set_requirements failed: {e}")
        raise


# ---------------------------------------------------------------------------
# Astra stage tools: run_fast_aero, generate_mesh, run_simulation,
# evaluate_results.  Session layout: runs/design_sessions/<sid>/<stage>/v<N>/
# ---------------------------------------------------------------------------

from services.vspaero import FastAeroRejected, run_fast_aero as fast_aero_svc
from services.bwb_mesh import MeshRejected, generate_mesh as mesh_svc
from services.simulate import SimulationFailed, compose_requirement, run_simulation as sim_svc
from services.flow_conditions import (
    build_flow_contract,
    load_flow_contract,
    validate_openfoam_viscosity,
    write_flow_contract,
)
from services.evaluate import evaluate_case
from domain_packs.bwb.schema import GeometryReport


def _load_json(path: str) -> Dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def _require_stage(session_id: str, stage: str) -> str:
    d = ds.latest_dir(session_id, stage)
    if not d:
        raise ValueError(f"no valid '{stage}' stage in session {session_id} — run it first")
    return d


class FastAeroRequest(BaseModel):
    session_id: str


@mcp.tool(name="run_fast_aero")
async def run_fast_aero(request: FastAeroRequest, ctx: Context) -> Dict[str, Any]:
    """Stage 3: fast vortex-lattice aero (VSPAERO) on the approved geometry.
    Seconds-fast alpha sweep -> CL_alpha, trim alpha for the mission CL, L/D,
    neutral point. This is the tier-1 screen and the plausibility reference the
    evaluator later checks OpenFOAM against. Review before meshing."""
    try:
        geo_dir = _require_stage(request.session_id, "geometry")
        plan_dir = _require_stage(request.session_id, "plan")
        report = GeometryReport.model_validate(
            _load_json(os.path.join(geo_dir, "geometry_report.json")))
        derived = MissionDerived.model_validate(
            _load_json(os.path.join(plan_dir, "mission_derived.json")))
        vdir = ds.new_version_dir(request.session_id, "fast_aero")
        await ctx.info(f"Running VSPAERO sweep in {vdir}")
        result = fast_aero_svc(report, derived, vdir)
        version = ds.record_version(request.session_id, "fast_aero", {
            "passed": True,
            **atmosphere_provenance(derived.atmosphere),
        })
        return {"session_id": request.session_id, "version": version,
                "alpha_trim_deg": result["alpha_trim_deg"],
                "ld_at_trim": result["ld_at_trim"],
                "cl_alpha_per_deg": result["cl_alpha_per_deg"],
                "x_np_m": result["x_np_m"], "gates": result["gates"],
                "fast_aero_dir": vdir}
    except FastAeroRejected as e:
        await ctx.error(str(e))
        raise
    except Exception as e:
        await ctx.error(f"run_fast_aero failed: {e}")
        raise


class GenerateMeshRequest(BaseModel):
    session_id: str
    level: str = Field(default="coarse", description="coarse | medium | fine")
    max_cells: float = Field(default=3e6)


@mcp.tool(name="generate_mesh")
async def generate_mesh(request: GenerateMeshRequest, ctx: Context) -> Dict[str, Any]:
    """Stage 4: deterministic farfield mesh (Gmsh) around the approved geometry,
    validated with checkMesh and rendered for review (slice + surface images).
    The resulting .msh is what the simulation stage hands to the Foam-Agent
    graph. Show the renders and stats to the user for approval before solving."""
    try:
        geo_dir = _require_stage(request.session_id, "geometry")
        report = GeometryReport.model_validate(
            _load_json(os.path.join(geo_dir, "geometry_report.json")))
        vdir = ds.new_version_dir(request.session_id, "mesh")
        await ctx.info(f"Meshing (level={request.level}) in {vdir}")
        mesh_report = mesh_svc(
            stl_path=report.artifacts["stl"], mac_m=report.mean_aero_chord_m,
            span_m=report.params.span_m, outdir=vdir, level=request.level,
            max_cells=request.max_cells)
        version = ds.record_version(request.session_id, "mesh",
                                    {"level": request.level, "passed": True})
        renders = {k: v for k, v in mesh_report["artifacts"].items()
                   if k.startswith("render_")}
        return {"session_id": request.session_id, "version": version,
                "stats": mesh_report["stats"], "gates": mesh_report["gates"],
                "renders": list(renders.values()), "msh": mesh_report["artifacts"]["msh"],
                "mesh_dir": vdir}
    except MeshRejected as e:
        await ctx.error(str(e))
        raise
    except Exception as e:
        await ctx.error(f"generate_mesh failed: {e}")
        raise


class RunSimulationRequest(BaseModel):
    session_id: str
    iterations: int = Field(default=600, description="SIMPLE iterations")


@mcp.tool(name="run_simulation")
async def run_simulation(request: RunSimulationRequest, ctx: Context) -> Dict[str, Any]:
    """Stage 5: OpenFOAM RANS via the EXISTING Foam-Agent graph (planner ->
    input_writer -> runner -> reviewer), fed the approved mesh through the
    custom-mesh path. Long-running (tens of minutes). The requirement is the
    plan's enriched text pinned to the approved mesh patches and the VSPAERO
    trim angle of attack."""
    try:
        plan_dir = _require_stage(request.session_id, "plan")
        geo_dir = _require_stage(request.session_id, "geometry")
        fa_dir = _require_stage(request.session_id, "fast_aero")
        mesh_dir = _require_stage(request.session_id, "mesh")
        derived = MissionDerived.model_validate(
            _load_json(os.path.join(plan_dir, "mission_derived.json")))
        geometry = GeometryReport.model_validate(
            _load_json(os.path.join(geo_dir, "geometry_report.json")))
        fast = _load_json(os.path.join(fa_dir, "fast_aero.json"))
        with open(os.path.join(plan_dir, "enriched_requirement.txt")) as f:
            enriched = f.read()
        vdir = ds.new_version_dir(request.session_id, "simulation")
        flow_conditions = build_flow_contract(
            derived,
            reference_length_m=geometry.mean_aero_chord_m,
            reference_area_m2=geometry.s_ref_m2,
        )
        flow_conditions_path = os.path.join(vdir, "flow_conditions.json")
        write_flow_contract(flow_conditions_path, flow_conditions)
        requirement = compose_requirement(
            enriched, derived.mission.cruise_speed_ms,
            fast["alpha_trim_deg"], request.iterations,
            flow_conditions=flow_conditions)
        await ctx.info(f"Running Foam-Agent graph in {vdir} (this takes a while)")
        result = sim_svc(
            requirement,
            os.path.join(mesh_dir, "bwb.msh"),
            vdir,
            flow_conditions_path=flow_conditions_path,
        )
        validate_openfoam_viscosity(result["case_dir"], flow_conditions)
        version = ds.record_version(request.session_id, "simulation", {
            "iterations": request.iterations,
            "passed": True,
            "atmosphere_source": flow_conditions.atmosphere.source,
            "atmosphere_dataset_sha256": (
                flow_conditions.atmosphere.dataset_sha256
            ),
            "kinematic_viscosity_m2s": (
                flow_conditions.atmosphere.kinematic_viscosity_m2s
            ),
        })
        return {"session_id": request.session_id, "version": version,
                "case_dir": result["case_dir"], "log_path": result["log_path"],
                "simulation_dir": vdir}
    except SimulationFailed as e:
        await ctx.error(f"{e}\n...{e.log_tail[-800:]}")
        raise
    except Exception as e:
        await ctx.error(f"run_simulation failed: {e}")
        raise


class EvaluateRequest(BaseModel):
    session_id: str
    extra_slices: Optional[List[str]] = Field(
        default=None, description="Additional slice normals for renders, e.g. ['z']")


@mcp.tool(name="evaluate_results")
async def evaluate_results(request: EvaluateRequest, ctx: Context) -> Dict[str, Any]:
    """Stage 6: judge the finished CFD run — convergence, plausibility vs the
    VSPAERO tier, mission-target check — and produce the visual report (force
    coefficient history, residuals, velocity slice, surface pressure). Returns
    a verdict: pass | revise-numerics | revise-design | reject. Show the
    verdict, metrics and renders to the user."""
    try:
        plan_dir = _require_stage(request.session_id, "plan")
        sim_dir = _require_stage(request.session_id, "simulation")
        fa_dir = _require_stage(request.session_id, "fast_aero")
        derived = MissionDerived.model_validate(
            _load_json(os.path.join(plan_dir, "mission_derived.json")))
        fast = _load_json(os.path.join(fa_dir, "fast_aero.json"))
        flow_conditions = load_flow_contract(
            os.path.join(sim_dir, "flow_conditions.json")
        )
        geo_dir = _require_stage(request.session_id, "geometry")
        geo = GeometryReport.model_validate(
            _load_json(os.path.join(geo_dir, "geometry_report.json")))
        ref = {"sref": geo.s_ref_m2, "cref": geo.mean_aero_chord_m,
               "magUInf": derived.mission.cruise_speed_ms,
               "rho": derived.air_density_kgm3,
               "alpha_deg": fast["alpha_trim_deg"],
               "cofr_x": fast["x_np_m"] - 0.08 * geo.mean_aero_chord_m}
        vdir = ds.new_version_dir(request.session_id, "results")
        report = evaluate_case(
            case_dir=os.path.join(sim_dir, "case"), outdir=vdir,
            required_cl=derived.required_cl, vlm_cl=derived.required_cl,
            extra_slices=request.extra_slices, ref=ref,
            flow_conditions=flow_conditions)
        version = ds.record_version(request.session_id, "results", {
            "verdict": report["verdict"],
            "atmosphere_source": flow_conditions.atmosphere.source,
            "atmosphere_dataset_sha256": (
                flow_conditions.atmosphere.dataset_sha256
            ),
        })
        return {"session_id": request.session_id, "version": version,
                "verdict": report["verdict"], "metrics": report["metrics"],
                "gates": report["gates"], "warnings": report["warnings"],
                "renders": [v for k, v in report["artifacts"].items()
                            if k.startswith("render_")],
                "results_dir": vdir}
    except Exception as e:
        await ctx.error(f"evaluate_results failed: {e}")
        raise


# ============================================================================
# Astra tool: open_cockpit — browser design workspace over the same session
# store. The cockpit applies revisions deterministically (no LLM); its queued
# explanations live in <sessions_root>/<sid>/cockpit/explanations.jsonl.
# ============================================================================

import subprocess
import urllib.request


class OpenCockpitRequest(BaseModel):
    """Open (starting if needed) the Aero Design Cockpit web UI."""
    session_id: Optional[str] = Field(
        default=None, description="Design session to open; omit for the most recent one")
    port: int = Field(default=8765, description="Cockpit server port")


class OpenCockpitResponse(BaseModel):
    url: str = Field(description="Give this link to the user — it opens the 3D cockpit")
    started: bool = Field(description="True if this call started the server")
    log_path: str
    note: str


def _cockpit_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


@mcp.tool(name="open_cockpit")
async def open_cockpit(request: OpenCockpitRequest, ctx: Context) -> OpenCockpitResponse:
    """Open the interactive Aero Design Cockpit: a browser-based 3D workspace over the
    SAME design session used by plan_design/build_geometry. The user can inspect the
    BWB, move mission/geometry sliders with instant equation-based preview, apply
    OpenVSP rebuilds, run VSPAERO, and compare versions. Starts the server on demand
    and returns a localhost URL — show it to the user as a clickable link. After the
    user works in the cockpit, read <sessions_root>/<sid>/cockpit/explanations.jsonl
    and narrate the queued changes in plain language."""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    log_path = os.path.join(os.path.dirname(ds.sessions_root()), "cockpit.log")
    started = False

    if not _cockpit_alive(request.port):
        await ctx.info(f"Starting cockpit server on port {request.port}")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a") as log:
            subprocess.Popen(
                [sys.executable, "-m", "src.cockpit.server", "--port", str(request.port)],
                cwd=repo_root, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
        # startup includes the heavy services/config import chain (FAISS index
        # load), which can take well over a minute on first start
        for _ in range(120):
            await asyncio.sleep(1.0)
            if _cockpit_alive(request.port):
                started = True
                break
        else:
            tail = ""
            if os.path.exists(log_path):
                with open(log_path) as f:
                    tail = f.read()[-800:]
            raise RuntimeError(f"cockpit server did not become healthy on port "
                               f"{request.port}; log tail:\n{tail}")

    url = f"http://localhost:{request.port}/"
    if request.session_id:
        url += f"?session={request.session_id}"
    return OpenCockpitResponse(
        url=url, started=started, log_path=log_path,
        note="Cockpit changes are applied deterministically (OpenVSP + gates, no LLM) "
             "and queued for chat narration in the session's cockpit/explanations.jsonl.")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="FastMCP OpenFOAM Agent Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="http",
        help="Transport method (default: http)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=7860,
        help="Port for HTTP transport (default: 7860)"
    )
    parser.add_argument(
        "--host",
        default="localhost",
        help="Host for HTTP transport (default: localhost)"
    )
    
    args = parser.parse_args()
    
    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        # Configure uvicorn with correct websockets setting
        uvicorn_config = {"ws": "websockets"}
        mcp.run("http", host=args.host, port=args.port, uvicorn_config=uvicorn_config)


# run the server:
# python -m src.mcp.fastmcp_server --transport http --host 0.0.0.0 --port 7860
