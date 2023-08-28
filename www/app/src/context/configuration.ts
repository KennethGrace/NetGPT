import { createContext, useContext } from "react";

// Server-Side Request API
export type NetworkSettings = {
  // The username of the user
  username: string;
  // The password of the user
  password: string;
  // The device type
  deviceType: string;
  // The enable secret password
  enablePassword?: string;
};

export type LanguageSettings = {
  name: string;
  description: string;
  fields: {
    [label: string]: string;
  };
};

export type PluginSettings = {
  name: string;
  description: string;
  fields: {
    [label: string]: string;
  };
  enabled: boolean;
};

export type Aliases = {
  [label: string]: string;
};

// The Application Configuration
export interface Configuration {
  // The server url
  serverUrl?: string;
  // Set the server url
  setServerUrl: (serverUrl: string) => void;
  // The credentials of the user
  networkSettings?: NetworkSettings;
  // Set the credentials of the user
  setNetworkSettings: (settings: NetworkSettings) => void;
  // The language settings
  languageSettings?: LanguageSettings;
  // Set the language settings
  setLanguageSettings: (settings: LanguageSettings) => void;
  // The aliases to use
  aliases: Aliases;
  // Set the aliases to use
  setAliases: (aliases: Aliases) => void;
}

export const DefaultConfiguration = {
  aliases: {},
};

export const ConfigurationContext = createContext<Configuration>(
  DefaultConfiguration as Configuration,
);

export const useConfiguration = () => useContext(ConfigurationContext);
