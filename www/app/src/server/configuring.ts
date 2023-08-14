import axios, { AxiosError } from "axios";

import { LanguageSettings } from "../common/configuration";

export type DeviceOptions = {
  options: string[];
};

export const fetchDeviceOptions = async (serverURL: string) => {
  const response = await axios.get<DeviceOptions>(`${serverURL}/settings/deviceTypes`).catch((error) => {
    if (axios.isAxiosError(error)) {
      const axiosError = error as AxiosError;
      if (axiosError.response) {
        // The request was made, but the server responded with a status code outside of the 2xx range
        throw new Error(axiosError.response.statusText);
      }
    }
    throw error;
  });
  return response.data;
};

export type LanguageSettingsList = {
  settings: LanguageSettings[];
};

export const fetchLanguageSettingsList = async (serverURL: string): Promise<LanguageSettingsList> => {
    const response = await axios.get<LanguageSettingsList>(`${serverURL}/settings/languages`).catch((error) => {
      if (axios.isAxiosError(error)) {
        const axiosError = error as AxiosError;
        if (axiosError.response) {
          // The request was made, but the server responded with a status code outside of the 2xx range
          throw new Error(axiosError.response.statusText);
        }
      }
      throw error;
    });
    return response.data;
};