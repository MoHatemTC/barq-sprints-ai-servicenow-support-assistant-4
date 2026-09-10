(function executeRule(current, previous /*null when async*/) {
    try {
        // Target your FastAPI endpoint (adjust hostname/port/tunnel URL accordingly)
        var endpointUrl = "https://abhorrently-threadless-reina.ngrok-free.dev/webhook";

        var request = new sn_ws.RESTMessageV2();
        request.setEndpoint(endpointUrl);
        request.setHttpMethod("POST");

        // Set Headers
        request.setRequestHeader("Accept", "application/json");
        request.setRequestHeader("Content-Type", "application/json");

        // Optional shared secret for verification
        request.setRequestHeader("X-ServiceNow-Secret", "barq-g4-secure-token");

        // Construct structured payload matching the Pydantic schema
        var payload = {
            sys_id: current.getValue("sys_id"),
            number: current.getValue("number"),
            short_description: current.getValue("short_description") || "",
            description: current.getValue("description") || "",
            category: current.getValue("category") || "inquiry",
            priority: parseInt(current.getValue("priority"), 10) || 3,
            caller_id: current.getValue("caller_id") || "",
            created_on: current.getValue("sys_created_on")
        };

        request.setRequestBody(JSON.stringify(payload));

        // Execute outbound call asynchronously from ServiceNow's scheduler queue
        var response = request.execute();
        var httpStatus = response.getStatusCode();

        if (httpStatus === 202) {
            gs.info("BARQ G4 Webhook: Successfully delivered incident " + current.getValue("number") + " (HTTP 202).");
        } else {
            gs.warn("BARQ G4 Webhook: Unexpected status code " + httpStatus + " for incident " + current.getValue("number") + ". Body: " + response.getBody());
        }
    } catch (ex) {
        gs.error("BARQ G4 Webhook Error in Business Rule: " + ex.getMessage());
    }
})(current, previous);
