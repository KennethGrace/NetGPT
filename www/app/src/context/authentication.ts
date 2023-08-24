import { createContext, useContext } from "react";

export type UserLoginInformation = {
  username: string;
  domain: string;
};

export type AuthenticationServerInformation = {
  provider: string;
  server: string;
  realm: string;
  clientId: string;
};

// The interface for the authentication context
export interface Authentication {
  // The server information
  authServer?: AuthenticationServerInformation;
  // Set the server information
  setAuthServer: (authServer: AuthenticationServerInformation) => void;
  // The authentication status
  isAuthenticated: boolean;
  // Set the authentication status
  setIsAuthenticated: (isAuthenticated: boolean) => void;
}

// The authentication context
export const AuthenticationContext = createContext<Authentication>({
  authServer: undefined,
  isAuthenticated: false,
} as Authentication);

// The authentication context provider
export const useAuthentication = () => useContext(AuthenticationContext);
