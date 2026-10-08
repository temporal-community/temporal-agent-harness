// Tool policy for the Cedar auto mode travel agent.
//
// A call no policy permits is escalated to the user, not refused: only an explicit `forbid`
// refuses. So `permit` says what may run unattended, and `forbid` says what must never run.
// Actions are "travel-agent/<tool>"; see schema.cedarschema for each one's parameters.

// ---------------------------------------------------------------- the script tool

// A script runs unattended unless a `forbid` below matches. Each host call it makes is still
// judged on its own, so a script that books once runs, and the booking comes to the user.
@id("scripts")
permit (principal, action == Action::"travel-agent/run_travel_code", resource)
when {
  context.parameters has arg0 &&
  context.parameters.arg0.data has dynamic_code &&
  !context.parameters.arg0.data.dynamic_code
};

// eval, exec and compile run code the analysis can't read, so a script using them is refused.
@id("no-dynamic-code")
forbid (principal, action == Action::"travel-agent/run_travel_code", resource)
when {
  context.parameters has arg0 &&
  context.parameters.arg0.data has dynamic_code &&
  context.parameters.arg0.data.dynamic_code
};

// Never book in a loop, concurrently, or more than once per script.
@id("no-bulk-flight-booking")
forbid (principal, action == Action::"travel-agent/run_travel_code", resource)
when {
  context.parameters has arg0 &&
  context.parameters.arg0.data has uses &&
  context.parameters.arg0.data.uses has book_flight &&
  (context.parameters.arg0.data.uses.book_flight.in_loop ||
   context.parameters.arg0.data.uses.book_flight.concurrent ||
   context.parameters.arg0.data.uses.book_flight.call_sites > 1)
};

@id("no-bulk-hotel-booking")
forbid (principal, action == Action::"travel-agent/run_travel_code", resource)
when {
  context.parameters has arg0 &&
  context.parameters.arg0.data has uses &&
  context.parameters.arg0.data.uses has book_hotel &&
  (context.parameters.arg0.data.uses.book_hotel.in_loop ||
   context.parameters.arg0.data.uses.book_hotel.concurrent ||
   context.parameters.arg0.data.uses.book_hotel.call_sites > 1)
};

// A script the classifier thinks could run away is refused.
@id("no-runaway-scripts")
forbid (principal, action == Action::"travel-agent/run_travel_code", resource)
when {
  context.parameters has arg0 &&
  context.parameters.arg0.data has classification &&
  context.parameters.arg0.data.classification has unbounded_iteration &&
  context.parameters.arg0.data.classification.unbounded_iteration >= 30
};

// ---------------------------------------------------------------- each host call

// Searching and summarizing run unattended.
@id("lookups")
permit (
  principal,
  action in [
    Action::"travel-agent/search_flights",
    Action::"travel-agent/search_hotels",
    Action::"travel-agent/get_trip_summary"
  ],
  resource
);

// Bookings are not permitted here, so each one comes to the user, except a booking with no
// name on it, which is refused.
@id("bookings-need-a-name")
forbid (principal, action == Action::"travel-agent/book_flight", resource)
unless {
  context.parameters has arg0 &&
  context.parameters.arg0.data has request &&
  context.parameters.arg0.data.request has passenger_name &&
  context.parameters.arg0.data.request.passenger_name != ""
};
