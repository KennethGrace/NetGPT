import Keycloak from "keycloak-js";

import axios, { AxiosError } from "axios";
import { AuthenticationServerInformation } from "../context/authentication";

// Validate the format of a server URL with a regex
export const validateServerUrl = (serverUrl: string) => {
  const regex = new RegExp("^(http|https)://[a-zA-Z0-9-.]+(:[0-9]{1,5})?/?$");
  return regex.test(serverUrl);
};

// This function will return a Keycloak instance
const getKeycloak = (info: AuthenticationServerInformation) => {
  return new Keycloak({
    url: info.server,
    realm: info.realm,
    clientId: info.clientId,
  });
};

const authenticate = async (info: AuthenticationServerInformation) => {
  const keycloak = getKeycloak(info);
  const authenticated = await keycloak.init({
    onLoad: "login-required",
    checkLoginIframe: false,
  });
  if (authenticated) {
    return keycloak;
  } else {
    throw new Error("Authentication failed");
  }
};

export const useLogin = async (info: AuthenticationServerInformation) => {
  const keycloak = await authenticate(info);
  await keycloak.login({
    redirectUri: window.location.href,
  });
  if (keycloak.token) {
    localStorage.setItem("token", keycloak.token);
    return keycloak;
  } else {
    throw new Error("No token found in login response");
  }
};

// the validate function will check if the token is still valid
// and if it is not, it will attempt to refresh the token
export const useValidation = async () => {
  // TODO: implement this
  const token = localStorage.getItem("token");
  if (token) {
  }
};

// Send a request to the server to get the server's login
// information. This will be used to determine what
// login options are available to the user.
export const fetchServerInformation = async (serverUrl: string) => {
  const response = await axios
    .get(`${serverUrl}/security/server`)
    .catch((error) => {
      if (axios.isAxiosError(error)) {
        const axiosError = error as AxiosError;
        if (axiosError.response) {
          // The request was made, but the server responded with a status code outside of the 2xx range
          throw new Error(axiosError.response.statusText);
        }
      }
      throw error;
    });
  return response.data as AuthenticationServerInformation;
};
