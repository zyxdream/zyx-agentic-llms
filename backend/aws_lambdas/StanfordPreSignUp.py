"""
Pre Sign-Up Lambda trigger for the Stanford Cognito User Pool.

Blocks federated (SAML) users whose eduPersonEntitlement attribute does not
contain at least one of the allowed Stanford Law values.
"""

REQUIRED_ENTITLEMENT = "law:saml"  

def lambda_handler(event, context):                                                                      
    if event.get("triggerSource") != "PreSignUp_ExternalProvider":
        return event

    user_attributes = event.get("request", {}).get("userAttributes", {})
    entitlement_raw = user_attributes.get("custom:eduPersonEntitlement", "")
    print("Entitlements received:", entitlement_raw)

    entitlements = {v.strip() for v in entitlement_raw.split(",") if v.strip()}

    if REQUIRED_ENTITLEMENT not in entitlements:
        raise Exception(
            "Access denied: your account does not have a Stanford Law entitlement."
        )

    return event
