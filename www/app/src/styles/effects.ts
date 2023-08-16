import { useState, useEffect } from 'react';

/**
* This hook is used to fade in an element over a given duration.
* It returns a tuple containing the style to apply to the element
* and a function to call to start the fade-in.
*
* @returns A tuple containing the style to apply to the element and a function to call to start the fade-in.
*
* @example
* const fadeIn, setFadeIn = useFadeIn();
*/
export const useFadeIn = () => {
    const [fadeIn, setFadeIn] = useState(false);

    useEffect(() => {
        if (fadeIn) {
            const timer = setTimeout(() => {
                setFadeIn(true);
            }, 2000);

            return () => {
                clearTimeout(timer);
            };
        }
    }, [fadeIn]);

    return [fadeIn, setFadeIn] as const;
}