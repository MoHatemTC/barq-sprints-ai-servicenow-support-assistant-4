(function executeRule(current, previous /* null when async */) {

    // 1. Read webhook configuration dynamically
    var webhookUrl = gs.getProperty(
        'x_2215387_sprint_0.webhook.url'
    );

    var secret = gs.getProperty(
        'x_2215387_sprint_0.webhook.secret'
    );

    // 2. Stop if configuration is missing
    if (!webhookUrl || !secret) {
        gs.error('S3.2: Webhook URL or secret is missing.');
        return;
    }

    // 3. Detect creation vs update
    var eventType;

    if (current.operation() == 'insert') {
        eventType = 'created';
    } else if (current.operation() == 'update') {
        eventType = 'updated';
    } else {
        // Async Business Rules can return null for operation().
        // Fall back to comparing creation/update timestamps.
        var createdOn = current.getValue('sys_created_on');
        var updatedOn = current.getValue('sys_updated_on');

        if (createdOn == updatedOn) {
            eventType = 'created';
        } else {
            eventType = 'updated';
        }
    }

    // 4. Build the JSON payload
    var payload = {
        incident_sys_id: current.getUniqueValue(),
        sys_id: current.getUniqueValue(),
        number: current.getValue('number'),
        short_description: current.getValue('short_description'),
        description: current.getValue('description'),
        event_type: eventType
    };

    // 5. Stringify the EXACT body that will be sent
    var body = JSON.stringify(payload);

    // 6. Generate HMAC-SHA256 signature
    var signature;

    try {
        var hmac = new HmacSha256();
        signature = hmac.calculate(secret, body);

        gs.info(
            'S3.2: HMAC generated for ' +
            current.getValue('number') +
            ' event_type=' +
            eventType +
            ' signature_length=' +
            signature.length
        );

    } catch (e) {

        gs.error(
            'S3.2: HMAC FAILED for ' +
            current.getValue('number') +
            ' error=' +
            e
        );

        return;
    }

    // 7. Create outbound REST request
    var request = new sn_ws.RESTMessageV2();

    request.setEndpoint(webhookUrl);
    request.setHttpMethod('post');

    // 8. Set request headers
    request.setRequestHeader(
        'Content-Type',
        'application/json'
    );

    request.setRequestHeader(
        'X-Signature',
        signature
    );

    // 9. Send the exact same body that was signed
    request.setRequestBody(body);

    // 10. Execute outbound request
    try {

        var response = request.execute();

        gs.info(
            'S3.2 outbound webhook for ' +
            current.getValue('number') +
            ' returned HTTP ' +
            response.getStatusCode()
        );

    } catch (e) {

        gs.error(
            'S3.2: Webhook request FAILED for ' +
            current.getValue('number') +
            ' error=' +
            e
        );
    }

})(current, previous);