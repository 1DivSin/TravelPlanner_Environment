---
name: travelplanner
description: Plan TravelPlanner benchmark itineraries directly with the provided local data tools. Use when the user explicitly asks to solve a TravelPlanner task with the travelplanner skill; do not use it to author or run a Workflow.
---

# TravelPlanner

Solve the current TravelPlanner request directly in the parent session. This
skill is the direct-execution counterpart of the `cc_dynamic_v1` TravelPlanner
Workflow treatment. Preserve its planning behavior and result contract; only
the execution carrier changes from Workflow Agent Steps to bounded work in the
current session.

## Execution Contract

1. Keep the work small and fast: use at most three logical phases and at most
   five bounded planning stages in total.
2. Prefer sequential phases. Group independent searches together conceptually,
   but do not create subagents, author a Workflow, or call `run_flow`.
3. A bounded stage may make at most five TravelPlanner tool calls. Across all
   stages, make at most 25 TravelPlanner tool calls. If a query returns no
   results, try one relevant alternative and then move on; do not loop or retry
   repeatedly.
4. Complete the task within ten minutes. Prefer a valid, complete plan produced
   promptly over open-ended optimization.
5. Use only the TravelPlanner data tools exposed by the session. Do not access
   the web, external benchmark answers, unrelated workspaces, or hidden data.

These are carrier adaptations only:

| Workflow treatment | Direct skill |
| --- | --- |
| up to three Workflow phases | up to three logical phases |
| up to five Agent Steps | up to five bounded planning stages |
| up to five tool calls per Agent Step | up to five tool calls per bounded stage |
| independent Agent Steps may run in parallel | independent searches may be grouped before dependent work |
| assemble and validate in downstream Agent Steps | assemble and validate in later direct stages |
| final Workflow Artifact | final response object |

## Planning Procedure

Use the same functional phases as the Workflow treatment:

1. Parse the origin, destination or state, dates, day count, traveler count,
   budget, number of cities, transport preference, room type, house rule,
   cuisine, and other explicit constraints. When the request names a state but
   not all cities, use `list_cities_in_state` before choosing the route.
2. Collect the transportation, accommodation, restaurant, and attraction data
   needed for the chosen route. Use `search_flights` for eligible flight legs,
   `compute_distance` for ground legs, and the city-specific search tools for
   local candidates. Keep exact source names, cities, times, prices, minimum
   stays, occupancy, room types, house rules, cuisines, and transport strings
   needed for selection and budget checks. Never invent a candidate.
3. Assemble the full day-by-day itinerary, then validate and repair it inline.
   Check the requested dates and exact day count; closed-circle route; transport
   preference and exact transport strings; city continuity; lodging night,
   minimum-stay, occupancy, room-type, and house-rule constraints; cuisine;
   meal and attraction coverage; restaurant variety when candidates permit;
   candidate membership; and total budget. Change only invalid selections or
   fields during the repair pass. Do not repeat collection merely to polish a
   valid plan.

## Result Contract

Return only one JSON object inside a Markdown `json` code block. Do not include
an acknowledgement, explanation, trace, or planning notes outside the block.

The object must contain exactly `idx`, `query`, and `plan`. Reproduce the
current request's `idx` and `query` exactly. `plan` must contain one object per
day with exactly these fields:

```json
{
  "idx": 1,
  "query": "the exact user query",
  "plan": [
    {
      "day": 1,
      "current_city": "from Origin to Destination",
      "transportation": "Flight Number: F1234567, from Origin to Destination, Departure Time: 09:00, Arrival Time: 11:00",
      "breakfast": "-",
      "attraction": "Attraction Name, City;Another Attraction, City;",
      "lunch": "Restaurant Name, City",
      "dinner": "Restaurant Name, City",
      "accommodation": "Accommodation Name, City"
    }
  ]
}
```

- `day` is a 1-indexed integer.
- On the first day, `current_city` is `from <origin> to <destination>`. On the
  last day, it is `from <current city> to <origin/home>`. Otherwise use the city
  name.
- `transportation` is the exact flight, self-driving, or taxi string supported
  by the tool data, or `-` when there is no inter-city travel that day.
- Meals and accommodation use `<Name>, <City>` or `-`.
- Attractions use semicolon-separated `<Name>, <City>;` entries or `-`.

If a usable candidate is unavailable, retain the required output shape and use
`-` only where the public contract allows it. Do not fabricate a resource to
make the plan appear complete.
