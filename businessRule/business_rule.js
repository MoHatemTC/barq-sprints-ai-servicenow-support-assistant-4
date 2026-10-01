(function executeRule(current, previous /*null when async*/) {
    try {
        var endpointUrl = "https://abhorrently-threadless-reina.ngrok-free.dev/api/v1/events/servicenow";

        var request = new sn_ws.RESTMessageV2();
        request.setEndpoint(endpointUrl);
        request.setHttpMethod("POST");

        request.setRequestHeader("Accept", "application/json");
        request.setRequestHeader("Content-Type", "application/json");

        var payload = {
            event_id: gs.generateGUID(),
            incident_sys_id: current.getValue("sys_id"),
            number: current.getValue("number"),
            short_description: current.getValue("short_description") || "",
            description: current.getValue("description") || ""
        };

        var requestBody = JSON.stringify(payload);

        var secret = gs.getProperty("x_2215387_sprint_0.webhook.secret");

        if (!secret) {
            gs.error("BARQ G4 Webhook: HMAC secret is not configured.");
            return;
        }

        var signature = new HmacSha256().calculate(secret, requestBody);

        request.setRequestHeader("X-Signature", signature);
        request.setRequestBody(requestBody);

        var response = request.execute();
        var httpStatus = response.getStatusCode();

        if (httpStatus === 202) {
            gs.info(
                "BARQ G4 Webhook: Successfully delivered incident " +
                current.getValue("number") +
                " (HTTP 202)."
            );
        } else {
            gs.warn(
                "BARQ G4 Webhook: Unexpected status code " +
                httpStatus +
                " for incident " +
                current.getValue("number") +
                ". Body: " +
                response.getBody()
            );
        }
    } catch (ex) {
        gs.error(
            "BARQ G4 Webhook Error in Business Rule: " +
            ex.getMessage()
        );
    }
})(current, previous);