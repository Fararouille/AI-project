# Single Sign-On and Provisioning

SSO is available as a Growth add-on and is included with Enterprise. Northwind
supports SAML 2.0 and SCIM 2.0.

## Supported identity providers

Okta, Microsoft Entra ID, Google Workspace, OneLogin, and any SAML 2.0 provider that
can sign assertions with RSA-SHA256. IdP-initiated and SP-initiated flows are both
supported.

## Setting up SAML

1. In Northwind, open **Settings > Security > SSO** and download the service provider
   metadata.
2. In your IdP, create a SAML application using that metadata.
3. Map the assertion attributes: `email` (required), `firstName`, `lastName`, `groups`.
4. Paste the IdP metadata back into Northwind and save.
5. Test with a single account before enforcing.

Saving SSO configuration does **not** enforce it. Enforcement is a separate toggle, and
turning it on signs out every session except the admin who enabled it. Keep a break-glass
local admin account until enforcement has been verified in production.

## Just-in-time provisioning

JIT provisioning creates a Northwind user on first successful SSO login. It is
**disabled by default**. When disabled, a user who authenticates successfully but has no
Northwind account receives `access_denied` - they must be invited first.

Enable JIT only if your IdP group membership is trustworthy enough to decide who gets
access.

## SCIM provisioning

SCIM handles create, update, and deactivate. Northwind supports the `/Users` and
`/Groups` resources. Group push maps IdP groups to Northwind roles:

- a group mapped to `admin` grants project administration
- a group mapped to `editor` grants dashboard editing
- unmapped groups grant nothing

Deactivation via SCIM removes access immediately but does not delete the user's data;
seat billing stops at the end of the current day. Periodic reconciliation runs every 24
hours and reports drift to project admins.

## Enforcement and exceptions

When enforcement is on, password login is disabled for every member except break-glass
admins. API keys are unaffected by SSO enforcement - they are service credentials, not
user sessions. Rotate them separately.

## Trial

SSO trials run for **30 days** on any paid plan. At the end of the trial, enforcement is
turned off automatically rather than locking your team out.
