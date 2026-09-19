// authConfig.js
// One place for the Cognito settings, so main.jsx (sign-in) and api.js
// (reading the token) can never disagree about which pool/client they use.
// These ids are public by design (they ship in every browser bundle); the
// Lambda is what actually verifies tokens.

const authority =
  import.meta.env.VITE_COGNITO_AUTHORITY ||
  "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_9Ea0gBsLs";

const client_id =
  import.meta.env.VITE_COGNITO_CLIENT_ID || "7s5gpf5rdfuh25fhav2ha09ja";

export const cognitoAuthConfig = {
  authority,
  client_id,
  // Works on localhost AND on the Amplify URL. Both must be listed under
  // "Allowed callback URLs" in the Cognito app client.
  redirect_uri: window.location.origin,
  response_type: "code",
  scope: "openid email",
  // Strip ?code=&state= from the URL after sign-in. Without this, a page
  // refresh replays a used code and shows "No matching state found".
  onSigninCallback: () => {
    window.history.replaceState({}, document.title, window.location.pathname);
  },
};

// Where react-oidc-context (oidc-client-ts) keeps the signed-in user.
// Default store is sessionStorage, key "oidc.user:<authority>:<client_id>".
export const OIDC_USER_KEY = `oidc.user:${authority}:${client_id}`;
