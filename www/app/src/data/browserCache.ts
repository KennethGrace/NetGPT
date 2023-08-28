import { LanguageSettings, NetworkSettings } from "../context/configuration";
import { AuthenticationServerInformation } from "../context/authentication";

export const saveServerUrl = (serverUrl: string) => {
  localStorage.setItem("serverUrl", serverUrl);
};

export const loadServerUrl = () => {
  const url = localStorage.getItem("serverUrl");
  if (url) {
    return url;
  }
  return undefined;
};

export const saveServerInformation = (
  serverInformation: AuthenticationServerInformation,
) => {
  localStorage.setItem("serverInformation", JSON.stringify(serverInformation));
};

export const loadServerInformation = ():
  | AuthenticationServerInformation
  | undefined => {
  const serverInformation = localStorage.getItem("serverInformation");
  if (serverInformation) {
    return JSON.parse(serverInformation);
  }
  return undefined;
};

export const saveNetworkSettings = (networkSettings: NetworkSettings) => {
  localStorage.setItem("networkSettings", JSON.stringify(networkSettings));
};

export const loadNetworkSettings = (): NetworkSettings | undefined => {
  const networkSettings = localStorage.getItem("networkSettings");
  if (networkSettings) {
    return JSON.parse(networkSettings);
  }
  return undefined;
};

export const saveLanguageSettings = (languageSettings: LanguageSettings) => {
  localStorage.setItem("languageSettings", JSON.stringify(languageSettings));
};

export const loadLanguageSettings = (): LanguageSettings | undefined => {
  const languageSettings = localStorage.getItem("languageSettings");
  if (languageSettings) {
    return JSON.parse(languageSettings);
  }
  return undefined;
};

export const saveGreeting = (greeting: string) => {
  localStorage.setItem("greeting", greeting);
};

export const loadGreeting = (): string | undefined => {
  const greeting = localStorage.getItem("greeting");
  if (greeting) {
    return greeting;
  }
  return undefined;
};
