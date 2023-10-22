import Keycloak from "keycloak-js";

import axios, {AxiosError} from "axios";
import {AuthenticationServerInformation} from "../context/authentication"; // Validate the format of a server URL with a regex

// Validate the format of a server URL with a regex
export const validateServerUrl = (serverUrl: string) => {
  const regex = new RegExp("^(https)://[a-zA-Z0-9-.]+(:[0-9]{1,5})?/?$");
  return regex.test(serverUrl);
};

const getKeycloak = async (info: AuthenticationServerInformation) => {
  const kc = new Keycloak({
    realm: info.realm,
    url: info.server,
    clientId: info.clientId,
  });
  await kc
    .init({
      onLoad: "login-required",
      enableLogging: true,
    })
    .catch((error) => {
      console.log(error.toString());
      return null;
    })
    .then((authenticated) => {
      if (authenticated) {
        console.log("User is authenticated");
      } else {
        console.log("User is not authenticated");
      }
    });
  return kc;
};

class Authenticator {
  private keycloakInstance: Keycloak | null;

  constructor() {
    this.keycloakInstance = null;
  }

  public async login(information: AuthenticationServerInformation) {
    if (!this.keycloakInstance) {
      this.keycloakInstance = await getKeycloak(information).catch((error) => {
        console.log(error.toString());
        return null;
      });
      localStorage.setItem("keycloak", JSON.stringify(this.keycloakInstance));
    } else {
      console.log("Keycloak instance already exists");
    }
  }

  public isInstanceInitialized() {
    return this.keycloakInstance !== null;
  }

  public async getToken() {
    if (this.keycloakInstance) {
      await this.keycloakInstance.updateToken(5).catch((error) => {
        console.log(error.toString());
        console.log("Error updating token");
      });
      return this.keycloakInstance.token ?? null;
    } else {
      console.log("No token. Keycloak instance is null");
    }
    return null;
  }

  public async isAuthenticated() {
    if (this.keycloakInstance) {
      console.log("Checking if user is authenticated");
      if (this.keycloakInstance.authenticated) {
        console.log("User is authenticated");
      } else {
        console.log("User is not authenticated");
      }
      return this.keycloakInstance.authenticated ?? false;
    } else {
      console.log("Cannot authenticate. Keycloak instance is null");
    }
    return false;
  }
}

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

export const AuthenticationHandler = new Authenticator();
