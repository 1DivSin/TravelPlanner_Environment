-- SCENARIO: Build and independently verify a 7-day, 3-city North Carolina trip plan from Indianapolis.
-- AUTHORED: 2026-09-06 17:31:43 from intent: "week-long travel plan for one person, Indianapolis to 3 cities in North Carolina, 2022-03-07 to 2022-03-13, budget $6,500"

-- @artifact trip_request = {"type":"object","description":"The traveler's task contract.","properties":{"idx":{"type":"integer","description":"Task index echoed into the final plan."},"query":{"type":"string","description":"Verbatim user query text.","minLength":1},"origin":{"type":"string","description":"Origin city.","minLength":1},"state":{"type":"string","description":"Destination state.","minLength":1},"city_count":{"type":"integer","description":"Number of distinct destination cities required.","minimum":1},"start_date":{"type":"string","description":"First trip date, YYYY-MM-DD.","minLength":10},"end_date":{"type":"string","description":"Last trip date, YYYY-MM-DD.","minLength":10},"days":{"type":"integer","description":"Total trip days.","minimum":1},"travelers":{"type":"integer","description":"Number of people traveling.","minimum":1},"budget":{"type":"number","description":"Total budget in US dollars.","minimum":0}},"required":["idx","query","origin","state","city_count","start_date","end_date","days","travelers","budget"],"additionalProperties":false}
-- @artifact travel_evidence [object]: Verified route, transportation options, accommodations, restaurants, and attractions gathered from the official task tools; the sole ground truth for planning.
-- @artifact draft_plan [object]: Candidate plan object with idx, query, and a 7-item plan array.
-- @artifact cost_breakdown [string]: Transport, lodging, and meal arithmetic with the grand total and remaining budget headroom.
-- @artifact verification_report [string]: Structured checks with PASS/FAIL/UNDETERMINED statuses, violations, and the OK or FIXED verdict.
-- @artifact final_plan [object]: The verified final plan object with idx, query, and the 7-item plan array.

const trip_request: Artifact;
const travel_evidence: Artifact;
const draft_plan: Artifact;
const cost_breakdown: Artifact;
const verification_report: Artifact;
const final_plan: Artifact;

const build_itinerary: Step;
const verify_itinerary: Step;

const planner_agent: Agent, Executor;
const verifier_agent: Agent, Executor;

workflow nc_trip_plan {
  -- DATA FLOW
  input_workflow(nc_trip_plan) == [trip_request, travel_evidence];
  consumes(build_itinerary) == [trip_request, travel_evidence];
  produces(build_itinerary) == [draft_plan, cost_breakdown];
  consumes(verify_itinerary) == [trip_request, travel_evidence, draft_plan, cost_breakdown];
  produces(verify_itinerary) == [verification_report, final_plan];
  output_workflow(nc_trip_plan) == [final_plan, verification_report];

  -- EXECUTOR ASSIGNMENT
  step_executor(build_itinerary) == planner_agent;
  step_executor(verify_itinerary) == verifier_agent;

  -- STEP CONFIGURATION
  step_name(build_itinerary) == "Build Itinerary";
  step_instruction(build_itinerary) == "./instructions/build_itinerary.md";
  step_timeout(build_itinerary) == 600;
  step_name(verify_itinerary) == "Verify Itinerary";
  step_instruction(verify_itinerary) == "./instructions/verify_itinerary.md";
  step_timeout(verify_itinerary) == 600;

  -- WORKFLOW CONFIGURATION
  workflow_timeout(nc_trip_plan) == 1500;
}
