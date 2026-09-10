(function executeRule(current, previous /*null when async*/) {
    try {
        var endpointUrl = "https://abhorrently-threadless-reina.ngrok-free.dev/webhook";

        var request = new sn_ws.RESTMessageV2();
        request.setEndpoint(endpointUrl);
        request.setHttpMethod("POST");

        request.setRequestHeader("Accept", "application/json");
        request.setRequestHeader("Content-Type", "application/json");
        request.setRequestHeader("X-ServiceNow-Secret", "barq-g4-secure-token");

        // Contract strictly trimmed per feedback
        var payload = {
            incident_sys_id: current.getValue("sys_id"),
            number: current.getValue("number"),
            short_description: current.getValue("short_description") || "",
            description: current.getValue("description") || ""
        };

        request.setRequestBody(JSON.stringify(payload));

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