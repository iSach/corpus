/* Native WebAuthn conversion for browsers without the newer JSON helpers. */
(function () {
  'use strict';

  function supported() {
    return Boolean(window.isSecureContext && window.PublicKeyCredential && navigator.credentials && navigator.credentials.create && navigator.credentials.get);
  }
  function decode(value) {
    const base64 = value.replace(/-/g, '+').replace(/_/g, '/');
    return Uint8Array.from(atob(base64 + '='.repeat((4 - base64.length % 4) % 4)), char => char.charCodeAt(0)).buffer;
  }
  function encode(value) {
    return btoa(String.fromCharCode(...new Uint8Array(value))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function optionsFromJSON(options, registration) {
    const result = { ...options, challenge: decode(options.challenge) };
    if (registration) result.user = { ...options.user, id: decode(options.user.id) };
    const key = registration ? 'excludeCredentials' : 'allowCredentials';
    if (options[key]) result[key] = options[key].map(credential => ({ ...credential, id: decode(credential.id) }));
    return result;
  }
  function credentialToJSON(credential) {
    if (!credential) throw new DOMException('No passkey was selected', 'NotAllowedError');
    const response = {};
    ['clientDataJSON', 'attestationObject', 'authenticatorData', 'signature', 'userHandle'].forEach(key => {
      if (credential.response[key] != null) response[key] = encode(credential.response[key]);
    });
    if (credential.response.getTransports) response.transports = credential.response.getTransports();
    return {
      id: credential.id, rawId: encode(credential.rawId), type: credential.type, response,
      authenticatorAttachment: credential.authenticatorAttachment || undefined,
      clientExtensionResults: credential.getClientExtensionResults()
    };
  }
  window.CorpusPasskeys = Object.freeze({
    supported,
    register: async options => credentialToJSON(await navigator.credentials.create({ publicKey: optionsFromJSON(options, true) })),
    authenticate: async (options, signal) => credentialToJSON(await navigator.credentials.get({ publicKey: optionsFromJSON(options, false), signal }))
  });
})();
