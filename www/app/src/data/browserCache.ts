import { NetworkSettings, LanguageSettings } from '../common/configuration';

export const saveServerUrl = (serverUrl: string) => {
    localStorage.setItem('serverUrl', serverUrl);
};

export const loadServerUrl = (): string | null => {
    return localStorage.getItem('serverUrl');
};

export const saveNetworkSettings = (networkSettings: NetworkSettings) => {
    localStorage.setItem('networkSettings', JSON.stringify(networkSettings));
};

export const loadNetworkSettings = (): NetworkSettings | undefined => {
    const networkSettings = localStorage.getItem('networkSettings');
    if (networkSettings) {
        return JSON.parse(networkSettings);
    }
    return undefined;
}

export const saveLanguageSettings = (languageSettings: LanguageSettings) => {
    localStorage.setItem('languageSettings', JSON.stringify(languageSettings));
}

export const loadLanguageSettings = (): LanguageSettings | undefined => {
    const languageSettings = localStorage.getItem('languageSettings');
    if (languageSettings) {
        return JSON.parse(languageSettings);
    }
    return undefined;
}
